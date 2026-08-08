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
import logging
import threading
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
_loop: Optional["asyncio.AbstractEventLoop"] = None
_loop_thread: Optional[int] = None


def _get_queue() -> "asyncio.Queue[Event]":
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def reset_queue() -> None:
    """Test helper — drop the queue/semaphore so fresh ones bind to the current loop."""
    global _queue, _worker_sem, _loop, _loop_thread
    _queue = None
    _worker_sem = None
    _loop = None
    _loop_thread = None


# Global cap on concurrently-running worker subprocesses. Per-task double-spawn
# is already prevented by _worker_in_flight; this bounds total parallelism so a
# fan-out (or many tasks) can't launch unbounded opencode processes at once.
_worker_sem: Optional["asyncio.Semaphore"] = None


def _get_worker_sem() -> "asyncio.Semaphore":
    global _worker_sem
    if _worker_sem is None:
        from .config import get_config
        n = int(get_config("max_concurrent_workers", 2) or 2)
        _worker_sem = asyncio.Semaphore(max(1, n))
    return _worker_sem


def emit(task_id: str, event_type: str) -> None:
    """Enqueue one event. Thread-safe: can be called from the daemon's event loop
    or from worker threads. If a loop is captured and this is called from another
    thread, uses call_soon_threadsafe; otherwise puts directly to the queue."""
    queue = _get_queue()
    item = Event(task_id, event_type)

    # If we have a captured loop and we're in a different thread, use threadsafe call
    if _loop is not None and _loop_thread is not None:
        current_thread = threading.current_thread().ident
        if current_thread != _loop_thread:
            _loop.call_soon_threadsafe(queue.put_nowait, item)
            return

    # Otherwise, call put_nowait directly (either no loop captured yet, or we're in the loop thread)
    queue.put_nowait(item)


async def dispatch_forever() -> None:
    """The daemon's background consumer: pull one event, run one orchestrator
    pass, forever. Started once at app startup (web/routes.py)."""
    global _loop, _loop_thread
    _loop = asyncio.get_running_loop()
    _loop_thread = threading.current_thread().ident

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


# The orchestrator is itself an Agent (seeded into the agents table). Its
# persona is the decision doctrine below; its tools are the decision tools it
# calls (one per pass) — served by web/agent_mcp.py, scoped by its agent token.
# There is NO direct model HTTP anywhere: the decision comes from a local CLI
# session, exactly like a worker's.
ORCHESTRATOR_TOOLS = [
    "get_context", "decide_spawn_worker", "decide_spawn_task",
    "decide_ask_human", "decide_complete", "decide_fail", "decide_note",
]

ORCHESTRATOR_PERSONA = """You are the Sisyphus orchestrator. You coordinate work \
on a task by choosing exactly ONE next action per turn. You never do the work \
yourself, and you never judge whether a worker's output is good enough — delegate \
that to a reviewer worker instead.

Read the task thread, then CALL exactly one decision tool:
- decide_spawn_worker(agent, instruction, why) — run a worker agent
  (e.g. "explorer", "engineer", "reviewer") on the task.
- decide_spawn_task(subtasks, why) — fan out across repos: one child task per
  {repo, goal}. Use when the work spans separate codebases (e.g. a ticket needing
  an iOS and an Android implementation). A worker's "plan" message lists the
  proposed splits — turn it into subtasks. Children report back as
  "[<status>] child ..." messages; a task whose children are all done is itself
  done (no separate reviewer needed); if any child failed, spawn a fix-child.
- decide_ask_human(question, why) — pause and ask the human.
- decide_complete(why) — the task is done. ONLY after a reviewer has APPROVED on
  the thread (or all children are done); otherwise spawn a reviewer instead.
- decide_fail(reason, why) — the task cannot be completed.
- decide_note(text, why) — record a thought and take another turn.

Messages prefixed with "[external]" are forge events about the change you shipped
(CI results, reviews, a merge): CI failed → spawn an engineer; changes requested
→ spawn an engineer; merged → decide_complete.

"why" is always a short one-line reason. Call the tool as a real invocation, not
as text; make exactly one call, then stop."""


def orchestrator_agent_name() -> str:
    """The agent that decides for a task. Today the single built-in
    'orchestrator'; a hook for per-task orchestrators later."""
    from . import agents
    orchestrators = agents.list_agents(kind="orchestrator")
    return orchestrators[0]["name"] if orchestrators else "orchestrator"


async def _decide(task: dict, thread_text: str) -> Decision:
    """Produce one decision by running the orchestrator AGENT (a local CLI
    session) — no direct model HTTP. The agent calls exactly one decision tool,
    which records the choice; we read it back here."""
    from .worker import spawn_decider

    raw = await spawn_decider(task["id"], orchestrator_agent_name(), thread_text)
    if not raw:
        # The decider produced no decision (crash/timeout/misbehaviour). Park via
        # ask_human rather than fail — a human reply re-triggers a fresh pass.
        return Decision(
            action="ask_human",
            question=("I couldn't produce a next-step decision (the orchestrator "
                      "agent returned nothing). Check the runtime, then reply to resume."),
            why="decider produced no decision",
        )
    try:
        return Decision(**raw)
    except ValidationError as exc:
        return Decision(action="fail", reason=f"invalid decision: {exc}", why="malformed decision from the agent")


def _pass_count(task_id: str) -> int:
    return sum(1 for m in store.get_thread(task_id) if m["author"] == "orchestrator")


def _budget_exceeded(task_id: str) -> Optional[str]:
    """A human-readable reason if this task (or today) has hit its $ cap, else
    None. Caps come from config; 0/unset disables that cap."""
    from .config import get_config
    from . import usage

    tb = float(get_config("task_budget_usd", 0) or 0)
    if tb > 0:
        c = usage.task_cost(task_id)
        if c >= tb:
            return f"task budget reached: ${c:.4f} spent of a ${tb:.2f} cap."
    db = float(get_config("daily_budget_usd", 0) or 0)
    if db > 0:
        c = usage.today_cost()
        if c >= db:
            return f"daily budget reached: ${c:.4f} spent today of a ${db:.2f} cap."
    return None


# Substrings core.worker.py's crash/timeout fallback message always contains
# — see spawn_worker's "ended without reporting a result" post. A run isolated
# from a broken model/runner still surfaces as "agent_no_result" via launcher.
_WORKER_FAILURE_MARKERS = ("ended without reporting", "agent_no_result", "failed to start")
MAX_CONSECUTIVE_WORKER_FAILURES = 3  # catch a broken runner/config in ~3 tries, not 25


def _consecutive_worker_failures(task_id: str) -> int:
    """How many of the most recent worker_finished messages, counting back
    from the latest, were crash/timeout fallbacks rather than a real
    finish/approve/request_changes result. A semantic counterpart to
    _consecutive_repeat: the LLM varies its rationale each pass even while
    re-spawning into the same broken runner, so a verbatim-text check alone
    won't catch this — this reads what actually happened, not what the
    orchestrator said about it.

    A human answer resets the chain: the human has looked at the failures
    (fixed the runner, pruned a worktree, ...) and asked to continue — without
    the reset, three stale failures would re-trip the breaker on every reply
    and the task could never be resumed."""
    relevant = [m for m in store.get_thread(task_id) if m["kind"] in ("worker_finished", "answer")]
    n = 0
    for m in reversed(relevant):
        if m["kind"] == "answer":
            break
        if any(marker in m["text"] for marker in _WORKER_FAILURE_MARKERS):
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


async def _terminate(task_id: str, status: str) -> None:
    """Set a terminal status AND reclaim the task's disk (worktrees, bare
    clones, run logs). Best-effort cleanup — never let it block termination."""
    task = store.get_task(task_id)
    store.set_status(task_id, status)
    try:
        from .worker import cleanup_task
        cleanup_task(task_id)
    except Exception:
        logger.exception("cleanup failed for task %s", task_id)

    # Advance the Mission Planner: if this task was launched from a plan item,
    # mark it done/failed (unblocks its dependents). Run in a thread to avoid
    # blocking the event loop on git subprocesses and AI conflict resolution.
    try:
        from . import planner
        await asyncio.to_thread(planner.on_task_terminal, task_id, status)
    except Exception:
        logger.exception("planner advance failed for task %s", task_id)

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

    # Budget guard: stop spending on a task (or for the day) once a configured
    # cap is hit — park it as waiting so a human decides. A human reply bypasses
    # the check for that one pass, so replying means "continue".
    if event_type != "human_replied":
        over = _budget_exceeded(task_id)
        if over:
            store.post_message(task_id, "orchestrator", kind="note",
                               text=f"[ask_human] budget guard — {over}")
            store.post_message(task_id, "orchestrator", kind="question",
                               text=f"{over} Reply to continue, or leave it parked.")
            store.set_status(task_id, "waiting")
            return Decision(action="ask_human", question=over, why="budget guard"), None

    # Single-agent mode: a task with a pinned agent skips the orchestrator
    # entirely — that one agent handles it solo, and its result completes the
    # task (no dispatch, no reviewer).
    if task.get("agent"):
        return await _handle_single_agent(task)

    if _pass_count(task_id) >= MAX_PASSES:
        store.post_message(task_id, "orchestrator", kind="note",
                           text="[fail] pass budget exceeded — stopping to avoid runaway cost")
        await _terminate(task_id, "failed")
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
        await _terminate(task_id, "failed")
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


async def _handle_single_agent(task: dict) -> tuple:
    """Drive a task pinned to one agent, with no orchestrator in the loop:
    spawn the agent once; when it reports back, its result completes the task
    (or its absence fails it). One shot — no dispatch, no reviewer, no retry."""
    task_id = task["id"]
    thread = store.get_thread(task_id)

    if not any(m["kind"] == "worker_started" for m in thread):
        store.set_status(task_id, "active")
        bg = asyncio.create_task(
            _spawn_worker_safe(task_id, task["agent"], task["goal"], task.get("repo"))
        )
        return Decision(action="spawn_worker", role=task["agent"],
                        why=f"single-agent task — running {task['agent']}"), bg

    # The agent has run. A reported result (finish/approve) → done; none → failed.
    if any(m["kind"] == "result" for m in thread):
        store.post_message(task_id, "orchestrator", kind="note",
                           text=f"[done] {task['agent']} completed the task")
        await _terminate(task_id, "done")
        return Decision(action="done", why="pinned agent completed"), None
    store.post_message(task_id, "orchestrator", kind="note",
                       text=f"[fail] {task['agent']} ended without a result")
    await _terminate(task_id, "failed")
    return Decision(action="fail", reason="pinned agent produced no result",
                    why="single-agent failure"), None


async def _spawn_worker_safe(task_id: str, role: str, instruction: str, repo_name):
    """Run a worker, but never let a startup failure (an unknown/hallucinated
    agent name, a clone error, …) vanish into a lost background task and leave
    the task stalled 'active'. Any exception before the worker reports is turned
    into a worker_finished + event, so the orchestrator re-decides."""
    try:
        # Bound total concurrency: wait for a free slot before launching the
        # subprocess. worker_started is only posted once inside spawn_worker, so
        # a task queued here stays correctly "not in flight" until it actually runs.
        async with _get_worker_sem():
            await spawn_worker(task_id, role, instruction, repo_name=repo_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("spawn_worker failed for task %s (role=%s)", task_id, role)
        store.post_message(task_id, "system", kind="worker_finished",
                           text=f"{role} failed to start: {exc}")
        emit(task_id, "worker_finished")


async def _execute(task_id: str, decision: Decision):
    if decision.action == "spawn_worker":
        store.set_status(task_id, "active")
        task = store.get_task(task_id)
        return asyncio.create_task(
            _spawn_worker_safe(task_id, decision.role, decision.instruction or "",
                               task.get("repo") if task else None)
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
        await _terminate(task_id, "done")
        return None
    if decision.action == "fail":
        await _terminate(task_id, "failed")
        return None
    return None  # note / noop: nothing further this pass


def reconcile_on_boot() -> int:
    """Daemon restart is a no-op. This single daemon is the ONLY process that
    ever runs a worker, so a restart always means any in-flight worker task is
    gone — no PID check is needed (unlike the old multi-process pipeline
    system). Any task whose last message is worker_started with nothing since
    is re-driven from its thread via a synthetic worker_finished event.

    Active tasks whose last message is anything else (a note, child_finished,
    a fresh created task, …) are stuck with no pending event to wake them.
    For those we queue a synthetic ``continue`` so the next dispatch pass
    re-evaluates them.

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
        elif thread:
            emit(task["id"], "continue")
            n += 1
    return n
