"""The orchestrator — a cold function: (thread + event) -> one decision.

There is no live orchestrator process and no state machine. Every event
triggers exactly one pass: read the task's thread, ask an LLM for ONE bounded
action, post the rationale, execute the action, done. Continuity comes
entirely from re-reading the thread — a daemon restart just means the next
event starts a fresh pass with the same memory (see reconcile_on_boot).

The orchestrator never judges a worker's output itself — "is this good/done?"
is expressed as spawn_worker(role="reviewer", ...), whose verdict comes back
as the next worker_finished event.
"""
import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from . import store
from .worker import spawn_worker

logger = logging.getLogger(__name__)

# The whole orchestrator vocabulary. It never grows without also growing the
# system prompt below — there is no other way to act on a task.
Action = Literal["spawn_worker", "ask_human", "note", "done", "fail", "noop"]

MAX_PASSES = 25              # per-task budget — a runaway task fails loudly
MAX_CONSECUTIVE_REPEATS = 3  # thrash circuit breaker


class Decision(BaseModel):
    action: Action
    why: str = ""
    role: Optional[str] = None
    instruction: Optional[str] = None
    question: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class Event:
    task_id: str
    type: str  # created | worker_finished | human_replied


_queue: Optional["asyncio.Queue[Event]"] = None


def _get_queue() -> "asyncio.Queue[Event]":
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def reset_queue() -> None:
    """Test helper — drop the queue so a fresh one binds to the current loop."""
    global _queue
    _queue = None


def emit(task_id: str, event_type: str) -> None:
    """Enqueue one event. Must be called from the daemon's main event loop —
    the same loop dispatch_forever() and every worker run on."""
    _get_queue().put_nowait(Event(task_id, event_type))


async def dispatch_forever() -> None:
    """The daemon's background consumer: pull one event, run one orchestrator
    pass, forever. Started once at app startup (web/routes.py)."""
    queue = _get_queue()
    while True:
        event = await queue.get()
        try:
            await handle_event(event.task_id, event.type)
        except Exception:
            logger.exception(
                "orchestrator pass failed for task %s (event=%s)", event.task_id, event.type
            )


SYSTEM_PROMPT = """You are the Sisyphus orchestrator. You coordinate work on a \
task by choosing exactly ONE next action. You never do the work yourself, and \
you never judge whether a worker's output is good enough — delegate that \
evaluation to a reviewer worker instead.

Reply with EXACTLY one JSON object and nothing else, matching one of:
{"action": "spawn_worker", "role": "explorer"|"engineer"|"reviewer", "instruction": "...", "why": "..."}
{"action": "ask_human", "question": "...", "why": "..."}
{"action": "note", "why": "..."}
{"action": "done", "why": "..."}
{"action": "fail", "reason": "...", "why": "..."}
{"action": "noop", "why": "..."}

"why" is always required: a short one-line reason for the choice.
"""


def _decide_via_llm_sync(thread_text: str) -> dict:
    """Ask the default runner's model for one decision. Synchronous (run via
    asyncio.to_thread) since it's a single blocking HTTP call, not a coding
    session — no opencode subprocess needed for this."""
    from .runners import get_default_runner

    runner = get_default_runner()
    if not runner:
        return {"action": "fail", "reason": "no runner configured", "why": "cannot decide without a model"}

    api_key = os.environ.get(runner.get("api_key_env", "") or "", "")
    base_url = (runner.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    model = runner.get("model") or "deepseek/deepseek-chat"

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": thread_text},
        ],
    }).encode()
    req = urllib.request.Request(
        base_url + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return {"action": "fail", "reason": f"orchestrator decision call failed: {exc}", "why": "decision error"}


async def _decide(task: dict, thread_text: str) -> Decision:
    raw = await asyncio.to_thread(_decide_via_llm_sync, thread_text)
    try:
        return Decision(**raw)
    except ValidationError as exc:
        return Decision(action="fail", reason=f"invalid decision: {exc}", why="malformed decision from the model")


def _pass_count(task_id: str) -> int:
    return sum(1 for m in store.get_thread(task_id) if m["author"] == "orchestrator")


def _consecutive_repeat(task_id: str) -> int:
    """How many times in a row the orchestrator posted the EXACT same note.

    Deliberately compares the full "[action] why" text, not just the action —
    spawn_worker repeating with a different role/rationale each time (e.g. an
    engineer/reviewer back-and-forth) is normal progress, not thrash. Only a
    verbatim-identical decision repeating is a genuine stuck loop. Reads the
    thread the orchestrator already writes — no extra schema needed."""
    notes = [m["text"] for m in store.get_thread(task_id) if m["author"] == "orchestrator"]
    if not notes:
        return 0
    last = notes[-1]
    n = 0
    for text in reversed(notes):
        if text == last:
            n += 1
        else:
            break
    return n


DecideFn = Callable[[dict, str], Awaitable[Decision]]


async def handle_event(
    task_id: str, event_type: str, decide_fn: Optional[DecideFn] = None,
) -> tuple:
    """One cold pass. Returns (decision, background_task_or_None) — the task
    is only non-None for spawn_worker, exposed so tests can await it."""
    task = store.get_task(task_id)
    if not task or task["status"] in ("done", "failed"):
        return Decision(action="noop", why="task missing or already terminal"), None

    if _pass_count(task_id) >= MAX_PASSES:
        store.post_message(task_id, "orchestrator", kind="note",
                           text="[fail] pass budget exceeded — stopping to avoid runaway cost")
        store.set_status(task_id, "failed")
        return Decision(action="fail", reason="pass budget exceeded", why="budget"), None

    thread_text = store.render_thread(task_id)
    decide = decide_fn or _decide
    decision = await decide(task, thread_text)

    store.post_message(task_id, "orchestrator", kind="note", text=f"[{decision.action}] {decision.why}")

    if _consecutive_repeat(task_id) > MAX_CONSECUTIVE_REPEATS:
        store.post_message(
            task_id, "orchestrator", kind="note",
            text=f"[fail] circuit breaker — {decision.action!r} repeated too many times",
        )
        store.set_status(task_id, "failed")
        return Decision(action="fail", reason="circuit breaker tripped", why="thrash"), None

    bg_task = await _execute(task_id, decision)
    return decision, bg_task


async def _execute(task_id: str, decision: Decision):
    if decision.action == "spawn_worker":
        store.set_status(task_id, "active")
        task = store.get_task(task_id)
        return asyncio.create_task(
            spawn_worker(task_id, decision.role, decision.instruction or "",
                        repo_name=task.get("repo") if task else None)
        )
    if decision.action == "ask_human":
        store.post_message(task_id, "orchestrator", kind="question", text=decision.question or "")
        store.set_status(task_id, "waiting")
        return None
    if decision.action == "done":
        store.set_status(task_id, "done")
        return None
    if decision.action == "fail":
        store.set_status(task_id, "failed")
        return None
    return None  # note / noop: nothing further this pass


def reconcile_on_boot() -> int:
    """Daemon restart is a no-op. This single daemon is the ONLY process that
    ever runs a worker, so a restart always means any in-flight worker task is
    gone — no PID check is needed (unlike the old multi-process pipeline
    system). Any task whose last message is worker_started with nothing since
    is re-driven from its thread via a synthetic worker_finished event.

    Returns the number of tasks reconciled."""
    n = 0
    for task in store.list_tasks(status="active"):
        thread = store.get_thread(task["id"])
        if thread and thread[-1]["kind"] == "worker_started":
            store.post_message(
                task["id"], "system", kind="worker_finished",
                text="orphaned by a daemon restart — resuming",
            )
            emit(task["id"], "worker_finished")
            n += 1
    return n
