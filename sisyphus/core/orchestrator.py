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
import time
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
Action = Literal["spawn_worker", "spawn_task", "ask_human", "note", "done", "fail", "noop"]

MAX_PASSES = 25              # per-task budget — a runaway task fails loudly
MAX_CONSECUTIVE_REPEATS = 3  # thrash circuit breaker


class Subtask(BaseModel):
    goal: str
    repo: str = ""   # empty = another repo-less child (nested planning)


class Decision(BaseModel):
    action: Action
    why: str = ""
    role: Optional[str] = None
    instruction: Optional[str] = None
    question: Optional[str] = None
    reason: Optional[str] = None
    subtasks: Optional[list[Subtask]] = None   # for spawn_task (fan-out)


@dataclass
class Event:
    task_id: str
    type: str  # created | worker_finished | human_replied | continue | external | child_finished


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
            # Never let an unexpected error leave a task silently stuck 'active'
            # with nothing to wake it — park it as waiting so it is visible and
            # a human reply can resume it.
            _park_on_error(event.task_id)


SYSTEM_PROMPT = """You are the Sisyphus orchestrator. You coordinate work on a \
task by choosing exactly ONE next action. You never do the work yourself, and \
you never judge whether a worker's output is good enough — delegate that \
evaluation to a reviewer worker instead.

Reply with EXACTLY one JSON object and nothing else, matching one of:
{"action": "spawn_worker", "role": "explorer"|"engineer"|"reviewer", "instruction": "...", "why": "..."}
{"action": "spawn_task", "subtasks": [{"repo": "ios-app", "goal": "..."}, {"repo": "android-app", "goal": "..."}], "why": "..."}
{"action": "ask_human", "question": "...", "why": "..."}
{"action": "note", "why": "..."}
{"action": "done", "why": "..."}
{"action": "fail", "reason": "...", "why": "..."}
{"action": "noop", "why": "..."}

"why" is always required: a short one-line reason for the choice.
Only choose "done" after a reviewer has APPROVED on the thread — if none has,
spawn a reviewer instead.

Use "spawn_task" to fan a task out across multiple repos: when the work spans
separate codebases (e.g. a ticket needing an iOS and an Android implementation),
create one child task per repo. A worker's "plan" message lists the proposed
{repo, goal} splits — turn it into subtasks. Each child then runs on its own.
Once children exist, they report back as "[<status>] child ..." messages: a task
whose children are all done is itself done (no separate reviewer needed); if any
child failed, spawn_task a fix-child on that repo or ask the human.

Messages prefixed with "[external]" are forge events about the change you shipped
(CI results, reviews, a merge). React to them:
- CI failed → spawn an engineer to diagnose and fix it.
- Reviewer requested changes / left comments → spawn an engineer to address them.
- The change was merged → the work has landed; choose "done".
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
    # A transient network hiccup on our OWN decision call must not permanently
    # fail (or silently stall) a task. Retry a few times, and on genuine
    # exhaustion park via ask_human rather than fail — a human reply re-triggers
    # a fresh decision. (URLError/socket.timeout/TimeoutError are all OSError.)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            content = payload["choices"][0]["message"]["content"]
            return json.loads(content)
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
            continue
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            # The model answered but not with a parseable decision — recoverable
            # by a human nudge, so don't permanently fail the task.
            return {
                "action": "ask_human",
                "question": "I couldn't read a clear next-step decision. Please advise, or reply to have me retry.",
                "why": f"unparseable decision: {exc}",
            }
    return {
        "action": "ask_human",
        "question": (
            f"My decision service was unreachable after retries ({last_err}). "
            "Check it, then reply here to resume."
        ),
        "why": "decision service unreachable",
    }


async def _decide(task: dict, thread_text: str) -> Decision:
    raw = await asyncio.to_thread(_decide_via_llm_sync, thread_text)
    try:
        return Decision(**raw)
    except ValidationError as exc:
        return Decision(action="fail", reason=f"invalid decision: {exc}", why="malformed decision from the model")


def _pass_count(task_id: str) -> int:
    return sum(1 for m in store.get_thread(task_id) if m["author"] == "orchestrator")


# Substrings core.worker.py's crash/timeout fallback message always contains
# — see spawn_worker's "ended without reporting a result" post. A run isolated
# from a broken model/runner still surfaces as "agent_no_result" via launcher.
_WORKER_FAILURE_MARKERS = ("ended without reporting", "agent_no_result")
MAX_CONSECUTIVE_WORKER_FAILURES = 3  # catch a broken runner/config in ~3 tries, not 25


def _consecutive_worker_failures(task_id: str) -> int:
    """How many of the most recent worker_finished messages, counting back
    from the latest, were crash/timeout fallbacks rather than a real
    finish/approve/request_changes result. A semantic counterpart to
    _consecutive_repeat: the LLM varies its rationale each pass even while
    re-spawning into the same broken runner, so a verbatim-text check alone
    won't catch this — this reads what actually happened, not what the
    orchestrator said about it."""
    finished = [m["text"] for m in store.get_thread(task_id) if m["kind"] == "worker_finished"]
    n = 0
    for text in reversed(finished):
        if any(marker in text for marker in _WORKER_FAILURE_MARKERS):
            n += 1
        else:
            break
    return n


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


def _worker_in_flight(task_id: str) -> bool:
    """True if a worker was spawned and hasn't reported back yet — i.e. the
    most recent worker_started has no worker_finished after it. Used to stop a
    mid-run event (e.g. a human reply) from spawning a SECOND worker for the
    same task; the running worker's completion drives the next decision, which
    will see whatever was posted in the meantime."""
    thread = store.get_thread(task_id)
    last_start = None
    for i, m in enumerate(thread):
        if m["kind"] == "worker_started":
            last_start = i
    if last_start is None:
        return False
    return not any(m["kind"] == "worker_finished" for m in thread[last_start + 1:])


def _reviewer_approved(task_id: str) -> bool:
    """True once a reviewer has posted an APPROVED result on the thread. The
    orchestrator never self-certifies quality (see the system prompt), so
    'done' is gated on this."""
    return any(
        m["author"] == "reviewer" and m["kind"] == "result" and m["text"].startswith("APPROVED")
        for m in store.get_thread(task_id)
    )


def _children_pending(task_id: str) -> bool:
    """True if this task fanned out and at least one child is still running.
    The parent waits (noop) until every child is terminal — the child_finished
    events wake it, so it never polls."""
    kids = store.list_children(task_id)
    return bool(kids) and any(c["status"] not in ("done", "failed") for c in kids)


def _all_children_done(task_id: str) -> bool:
    """True if this task fanned out and every child succeeded. A coordinator
    task produces no code, so this — not a reviewer — gates its 'done'."""
    kids = store.list_children(task_id)
    return bool(kids) and all(c["status"] == "done" for c in kids)


def _terminate(task_id: str, status: str) -> None:
    """Set a terminal status AND reclaim the task's disk (worktrees, bare
    clones, run logs). Best-effort cleanup — never let it block termination."""
    task = store.get_task(task_id)
    store.set_status(task_id, status)
    try:
        from .worker import cleanup_task
        cleanup_task(task_id)
    except Exception:
        logger.exception("cleanup failed for task %s", task_id)

    # Roll up to a fan-out parent: a finishing child notifies its parent's
    # thread and wakes it, exactly as a worker posts worker_finished and wakes
    # its task. The parent's next pass re-reads the thread and re-decides.
    if task and task.get("parent_id"):
        store.post_message(
            task["parent_id"], "system", kind="child_finished",
            text=f"[{status}] child '{task['goal']}' ({task['repo'] or 'no repo'})",
        )
        emit(task["parent_id"], "child_finished")


def _park_on_error(task_id: str) -> None:
    """Park an active task as 'waiting' after an unexpected pass error, so it's
    visible and resumable instead of silently stuck."""
    task = store.get_task(task_id)
    if not task or task["status"] in ("done", "failed"):
        return
    store.post_message(task_id, "orchestrator", kind="note",
                       text="[error] an internal error interrupted this pass — parked for review")
    store.post_message(task_id, "orchestrator", kind="question",
                       text="Something went wrong on my side. Reply here to resume, or check the daemon logs.")
    store.set_status(task_id, "waiting")


DecideFn = Callable[[dict, str], Awaitable[Decision]]


async def handle_event(
    task_id: str, event_type: str, decide_fn: Optional[DecideFn] = None,
) -> tuple:
    """One cold pass. Returns (decision, background_task_or_None) — the task
    is only non-None for spawn_worker, exposed so tests can await it."""
    task = store.get_task(task_id)
    if not task or task["status"] in ("done", "failed"):
        return Decision(action="noop", why="task missing or already terminal"), None

    # A worker is still running — don't start a second one. Its worker_finished
    # event drives the next decision, which will see anything posted meanwhile
    # (e.g. a human reply). This makes mid-run events safe no-ops.
    if _worker_in_flight(task_id):
        return Decision(action="noop", why="worker already in flight"), None

    # Fanned out and children are still running — wait. Their child_finished
    # events wake this parent; the last one to finish lets the pass proceed.
    if _children_pending(task_id):
        return Decision(action="noop", why="waiting on child tasks"), None

    if _pass_count(task_id) >= MAX_PASSES:
        store.post_message(task_id, "orchestrator", kind="note",
                           text="[fail] pass budget exceeded — stopping to avoid runaway cost")
        _terminate(task_id, "failed")
        return Decision(action="fail", reason="pass budget exceeded", why="budget"), None

    if _consecutive_worker_failures(task_id) >= MAX_CONSECUTIVE_WORKER_FAILURES:
        question = (
            "The last few worker runs failed without reporting a result "
            "(agent_no_result) — the runner or its config may be broken. "
            "Please check before I try again."
        )
        store.post_message(task_id, "orchestrator", kind="note",
                           text="[ask_human] workers keep failing to report a result")
        store.post_message(task_id, "orchestrator", kind="question", text=question)
        store.set_status(task_id, "waiting")
        return Decision(action="ask_human", question=question, why="repeated worker failures"), None

    thread_text = store.render_thread(task_id, for_prompt=True)
    decide = decide_fn or _decide
    decision = await decide(task, thread_text)

    # The orchestrator must not self-certify quality — a 'done' needs a
    # reviewer's approval on the thread. Otherwise, spawn one.
    if decision.action == "done" and not (_reviewer_approved(task_id) or _all_children_done(task_id)):
        decision = Decision(
            action="spawn_worker", role="reviewer",
            instruction=(f"Verify the task goal is fully met: {task['goal']}. "
                         "Approve if it is; otherwise request changes with specifics."),
            why="a reviewer must approve before done",
        )

    store.post_message(task_id, "orchestrator", kind="note", text=f"[{decision.action}] {decision.why}")

    if _consecutive_repeat(task_id) > MAX_CONSECUTIVE_REPEATS:
        store.post_message(
            task_id, "orchestrator", kind="note",
            text=f"[fail] circuit breaker — {decision.action!r} repeated too many times",
        )
        _terminate(task_id, "failed")
        return Decision(action="fail", reason="circuit breaker tripped", why="thrash"), None

    bg_task = await _execute(task_id, decision)

    # A pure think/no-op leaves the task active with nothing to wake it — re-emit
    # so the loop takes another pass. Identical repeats trip the thrash breaker;
    # the pass budget bounds a wandering one. Either way, never a silent stall.
    if decision.action in ("note", "noop"):
        cur = store.get_task(task_id)
        if cur and cur["status"] == "active":
            emit(task_id, "continue")

    return decision, bg_task


async def _execute(task_id: str, decision: Decision):
    if decision.action == "spawn_worker":
        store.set_status(task_id, "active")
        task = store.get_task(task_id)
        return asyncio.create_task(
            spawn_worker(task_id, decision.role, decision.instruction or "",
                        repo_name=task.get("repo") if task else None)
        )
    if decision.action == "spawn_task":
        # Fan out: one child task per repo, each running the normal loop. The
        # parent stays active and waits for their child_finished roll-ups.
        store.set_status(task_id, "active")
        for sub in (decision.subtasks or []):
            child = store.create_task(sub.goal, sub.repo or "", parent_id=task_id)
            store.post_message(
                task_id, "orchestrator", kind="note",
                text=f"[spawn_task] child {child['id']}: {sub.goal} ({sub.repo or 'no repo'})",
            )
            emit(child["id"], "created")
        return None
    if decision.action == "ask_human":
        store.post_message(task_id, "orchestrator", kind="question", text=decision.question or "")
        store.set_status(task_id, "waiting")
        return None
    if decision.action == "done":
        _terminate(task_id, "done")
        return None
    if decision.action == "fail":
        _terminate(task_id, "failed")
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
