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
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from . import heartbeat, store
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


def queue_depth() -> int:
    """How many events are waiting for a dispatcher pass. 0 with the loop idle
    is the normal, healthy reading."""
    try:
        return _get_queue().qsize()
    except RuntimeError:
        return 0             # no running loop — nothing is dispatching anyway


def reset_queue() -> None:
    """Test helper — drop the queue/gate so fresh ones bind to the current loop."""
    global _queue, _worker_gate, _loop, _loop_thread
    _queue = None
    _worker_gate = None
    _loop = None
    _loop_thread = None


# Global cap on concurrently-running worker subprocesses. Per-task double-spawn
# is already prevented by _worker_in_flight; this bounds total parallelism so a
# fan-out (or many tasks) can't launch unbounded opencode processes at once.
class _PriorityGate:
    """A semaphore that wakes review waiters before everyone else.

    With two slots and a stream of engineers, a queued reviewer used to wait
    ~10 minutes behind long engineering runs — the cheap, read-only step that
    unblocks a whole task sat at the back of a FIFO. Same total cap as before;
    only the wake order changes. Single-loop asyncio: no locking needed."""

    def __init__(self, n: int):
        self._free = max(1, n)
        self._waiters = {True: deque(), False: deque()}   # high? → futures

    async def acquire(self, high: bool = False) -> None:
        if self._free > 0 and not self._waiters[True] and not self._waiters[False]:
            self._free -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters[high].append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()                      # granted, but we cannot use it
            else:
                try:
                    self._waiters[high].remove(fut)
                except ValueError:
                    pass
            raise

    def release(self) -> None:
        for high in (True, False):
            q = self._waiters[high]
            while q:
                fut = q.popleft()
                if not fut.done():
                    fut.set_result(None)            # hand the slot over directly
                    return
        self._free += 1


def _is_review_role(role: str) -> bool:
    """Review-ish workers ride the fast lane; everything else queues normally."""
    return "review" in (role or "").lower()


_worker_gate: Optional[_PriorityGate] = None


def _get_worker_gate() -> _PriorityGate:
    global _worker_gate
    if _worker_gate is None:
        from .config import get_config
        n = int(get_config("max_concurrent_workers", 2) or 2)
        _worker_gate = _PriorityGate(max(1, n))
    return _worker_gate


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
    heartbeat.register("dispatcher", kind="event", every=0,
                       note="waiting for an event")
    while True:
        event = await queue.get()
        try:
            await handle_event(event.task_id, event.type)
            heartbeat.beat("dispatcher", note=f"last: {event.type} · {event.task_id}")
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

Before the FIRST spawn on a fresh task, judge the goal itself. If a competent \
implementer would have to make a significant assumption — about scope, behavior, \
or a tradeoff — call decide_ask_human with 1-3 concrete questions, each carrying \
a proposed default ("unless you say otherwise, I will do X"), so one short reply \
unblocks the task. At most one round of questions, at the start only. Ask ONLY \
where a wrong guess would waste the whole run; smaller assumptions are not \
questions — pass them to the worker in the instruction so it can name them in \
its restatement. Write questions to the human in the language the task goal is \
written in.

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


DECIDER_ATTEMPTS = 3


async def _decide(task: dict, thread_text: str) -> Decision:
    """Produce one decision by running the orchestrator AGENT (a local CLI
    session) — no direct model HTTP. The agent calls exactly one decision tool,
    which records the choice; we read it back here.

    An empty or malformed run is retried before anything is parked or failed:
    one flaky model response used to send the task straight to `waiting` (four
    times in the ledger), where it sat until a human noticed — retrying is
    cheap next to that. Only exhausted retries reach the old fallbacks."""
    from .worker import spawn_decider

    malformed: Optional[str] = None
    for attempt in range(1, DECIDER_ATTEMPTS + 1):
        raw = await spawn_decider(task["id"], orchestrator_agent_name(), thread_text)
        if not raw:
            malformed = None
            logger.warning("decider attempt %d/%d for %s produced no decision",
                           attempt, DECIDER_ATTEMPTS, task["id"])
            continue
        try:
            return Decision(**raw)
        except ValidationError as exc:
            malformed = str(exc)
            logger.warning("decider attempt %d/%d for %s was malformed: %s",
                           attempt, DECIDER_ATTEMPTS, task["id"], exc)

    if malformed is not None:
        return Decision(action="fail", reason=f"invalid decision: {malformed}",
                        why="malformed decision from the agent")
    # Every attempt came back empty (crash/timeout/misbehaviour). Park via
    # ask_human rather than fail — a human reply re-triggers a fresh pass.
    return Decision(
        action="ask_human",
        question=(f"I couldn't produce a next-step decision (the orchestrator "
                  f"agent returned nothing, {DECIDER_ATTEMPTS} attempts). "
                  "Check the runtime, then reply to resume."),
        why="decider produced no decision",
    )


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


SINGLE_AGENT_ATTEMPTS = 2   # one retry: a flaky run is cheap next to a dead task


def _runs_ended_since_human(task_id: str) -> int:
    """How many worker runs have ENDED since the last human answer.

    Counts worker_finished, not the wording of a failure: this is the number of
    times we have actually spent a run, which is what a retry budget is really
    about. A human answer resets it — the human has looked and asked to
    continue, exactly as in _consecutive_worker_failures.
    """
    n = 0
    for m in reversed(store.get_thread(task_id)):
        if m["kind"] == "answer":
            break
        if m["kind"] == "worker_finished":
            n += 1
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
        if m["kind"] in ("worker_started", "worker_queued"):
            last_start = i
    if last_start is None:
        return False
    return not any(m["kind"] == "worker_finished" for m in thread[last_start + 1:])


def _reviewer_approved(task_id: str) -> bool:
    """True once a reviewer has posted an APPROVED result on the thread. The
    orchestrator never self-certifies quality (see the system prompt), so
    'done' is gated on this. The verdict must lead the text (normalized for
    case/whitespace) and must not be negated ("NOT APPROVED")."""
    def _is_approved(text: str) -> bool:
        t = (text or "").lstrip().upper()
        return t.startswith("APPROVED") and not t.startswith("NOT APPROVED")

    return any(
        m["author"] == "reviewer" and m["kind"] == "result" and _is_approved(m["text"])
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
    # One statement of "may the loop act on this" (see pass_blocked_by): a
    # running worker must not get a second one spawned behind it, and a parent
    # mid-fan-out waits for its children's child_finished roll-ups. Not
    # spontaneous — an event brought us here, so a parked `waiting` task is
    # allowed to proceed; that is how a human reply resumes it.
    blocked = pass_blocked_by(task, spontaneous=False, event_type=event_type)
    if blocked:
        return Decision(action="noop", why=blocked), None

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
                         "Start your final result with the single word APPROVED "
                         "or with CHANGES REQUESTED."),
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
    spawn the agent once; when it reports back, its result completes the task.
    No dispatch and no reviewer — but a run that ends with nothing to show is
    retried once (SINGLE_AGENT_ATTEMPTS) before the task is failed."""
    task_id = task["id"]
    thread = store.get_thread(task_id)

    from .worker import was_stopped_by_a_human

    # A run that a human stopped produced no result, but that is not the same as
    # having nothing to show — the work was interrupted mid-instruction. Failing
    # it here made a stopped pinned task permanently unresumable: replying
    # re-entered this branch and failed it again. Re-run instead; the worker
    # sees the whole thread, so the human's new instruction comes with it.
    stopped = was_stopped_by_a_human(thread)
    if stopped:
        store.set_status(task_id, "active")
        bg = asyncio.create_task(
            _spawn_worker_safe(task_id, task["agent"], task["goal"], task.get("repo"))
        )
        return Decision(
            action="spawn_worker", role=task["agent"],
            why=f"single-agent task — re-running {task['agent']} after a human stop"), bg

    # A reported result (finish/approve) → done.
    if any(m["kind"] == "result" for m in thread):
        store.post_message(task_id, "orchestrator", kind="note",
                           text=f"[done] {task['agent']} completed the task")
        await _terminate(task_id, "done")
        return Decision(action="done", why="pinned agent completed"), None

    # Nothing to show — either the agent has not run yet, or it ran and reported
    # nothing. Both are answered by spending a run, and both are bounded by the
    # same budget: the number of runs already ended (plus the task budget guard
    # above), so a broken runner still stops instead of respawning forever.
    #
    # The half that changed: a run that ended empty used to fail the task on the
    # spot, and six of the seven failures in the ledger died exactly there — an
    # mr-reviewer that hit its idle timeout or never called its finishing tool,
    # on a review that then never happened until a human noticed. A silent
    # subprocess is not a verdict about the work. The retry re-reads the whole
    # thread, so the second attempt starts knowing how the first one died.
    runs = _runs_ended_since_human(task_id)
    if runs < SINGLE_AGENT_ATTEMPTS:
        store.set_status(task_id, "active")
        bg = asyncio.create_task(
            _spawn_worker_safe(task_id, task["agent"], task["goal"], task.get("repo"))
        )
        if runs:
            why = (f"{task['agent']} ended without a result — retrying "
                   f"(attempt {runs + 1} of {SINGLE_AGENT_ATTEMPTS})")
            store.post_message(task_id, "orchestrator", kind="note", text=f"[retry] {why}")
        else:
            why = f"single-agent task — running {task['agent']}"
        return Decision(action="spawn_worker", role=task["agent"], why=why), bg

    store.post_message(
        task_id, "orchestrator", kind="note",
        text=(f"[fail] {task['agent']} ended without a result "
              f"({runs} attempt{'' if runs == 1 else 's'})"))
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
        # subprocess. The queued marker keeps the wait visible — to the human,
        # and to the decider, whose passes would otherwise see "no worker in
        # flight" and spawn duplicates (a live failure: 8 phantom spawns while
        # two other tasks held both slots). _worker_in_flight treats a trailing
        # worker_queued as in flight.
        store.post_message(task_id, "system", kind="worker_queued",
                           text=f"{role} queued — waiting for a free worker slot; do not spawn again")
        gate = _get_worker_gate()
        await gate.acquire(high=_is_review_role(role))
        try:
            await spawn_worker(task_id, role, instruction, repo_name=repo_name)
        finally:
            gate.release()
    except Exception as exc:  # noqa: BLE001
        logger.exception("spawn_worker failed for task %s (role=%s)", task_id, role)
        store.post_message(task_id, "system", kind="worker_finished",
                           text=f"{role} failed to start: {exc}")
        emit(task_id, "worker_finished")


async def _execute(task_id: str, decision: Decision):
    if decision.action == "spawn_worker":
        store.set_status(task_id, "active")
        task = store.get_task(task_id)
        # Claim the cancel event HERE, synchronously, not inside the worker.
        # create_task returns before the coroutine runs its first line, so a
        # Stop landing in that window used to find nothing registered: the task
        # parked, and the worker then started anyway against a task the human
        # had already stopped.
        from . import cancellation
        cancellation.register(task_id, decision.role or "")
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
    system). Any task with an in-flight worker (a worker_started/worker_queued
    with no worker_finished after it — the worker may have posted progress
    notes since, so the thread TAIL alone can't be trusted) is re-driven via a
    synthetic worker_finished event; without it the dispatch guard would wait
    forever for a report from a process that no longer exists.

    Active tasks with no worker in flight (a note, child_finished, a fresh
    created task, …) are stuck with no pending event to wake them. For those
    we queue a synthetic ``continue`` so the next dispatch pass re-evaluates
    them.

    Correction: the worker *process* does not reliably die with the daemon
    (see core.sessions) — so before re-driving anything, kill whatever survived.
    Order matters: re-driving spawns a fresh worker on the same git worktree,
    and two runners writing one worktree is a worse failure than the orphan
    was. A reaper fault must not stop the daemon from booting, hence the guard.

    Returns the number of tasks reconciled."""
    from . import orphans
    try:
        orphans.reap()
    except Exception:  # noqa: BLE001
        logger.exception("orphan reaper failed — surviving workers may still be running")

    n = 0
    for task in store.list_tasks(status="active"):
        if _worker_in_flight(task["id"]):
            # A queued spawn dies with the daemon just like a running one.
            store.post_message(
                task["id"], "system", kind="worker_finished",
                text="orphaned by a daemon restart — resuming",
            )
            emit(task["id"], "worker_finished")
            n += 1
        else:
            # Covers the empty-thread case too: a task created just before a
            # crash has no messages yet and no event to wake it.
            emit(task["id"], "continue")
            n += 1
    return n


# ── Stall watchdog ──────────────────────────────────────────────────────
# A task only moves when an event is queued for it, and the queue lives in
# memory. Anything that creates a task without emitting leaves it `active` with
# nothing to wake it — a live review task sat that way for 2h13m, and only an
# unrelated daemon restart rescued it, because reconcile_on_boot ran. Nothing
# structural stops the next caller from making the same mistake, so treat a
# missing event as a fault to recover from rather than one to prevent
# call-site by call-site.

STALL_AFTER_SECONDS = 600
WATCHDOG_INTERVAL_SECONDS = 120


RESUMES_A_PARKED_TASK = ("human_replied", "external")


def pass_blocked_by(task: dict, spontaneous: bool = False,
                    event_type: Optional[str] = None) -> Optional[str]:
    """Why this task is NOT owed an orchestrator pass, or None if it is.

    The single statement of "should the loop act on this". It was previously
    written twice — inline in handle_event and again in find_stalled — and the
    copies had already drifted: find_stalled did not check for pending children,
    so a parent waiting on its fan-out could be reported as stalled and logged as
    a fault.

    ``spontaneous`` is the distinction the two callers genuinely need, and it is
    not cosmetic. A `waiting` task is parked ON PURPOSE, so nothing may wake it
    of its own accord — but a human reply must resume it, and task_reply emits
    `human_replied` WITHOUT first setting the status back to active
    (web/routes.py). So an event-driven pass has to accept a waiting task, while
    a scan must skip it. Collapsing the two would silently kill every
    human-answers-a-question resume.
    """
    if not task or task["status"] in ("done", "failed"):
        return "task missing or already terminal"
    if spontaneous and task["status"] != "active":
        return f"parked as {task['status']} — only an explicit event resumes it"
    # `waiting` means waiting on a HUMAN, so only a human (or an external
    # signal) may restart it. Without this, stopping a task cannot hold: the
    # cancelled worker posts worker_finished on its way out, that event drives
    # a pass, and the orchestrator immediately spawns the next worker.
    if (not spontaneous and task["status"] == "waiting"
            and event_type is not None and event_type not in RESUMES_A_PARKED_TASK):
        return f"parked for a human — {event_type!r} does not resume it"
    if _worker_in_flight(task["id"]):
        return "worker already in flight"
    if _children_pending(task["id"]):
        return "waiting on child tasks"
    return None


def find_stalled(stall_after: int = STALL_AFTER_SECONDS) -> list:
    """Active tasks owed a pass that have had no thread activity for
    *stall_after* seconds. ``updated_at`` is bumped by every post_message, so a
    frozen one means genuinely nothing is happening — not merely a quiet worker.
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    stalled = []
    for task in store.list_tasks(status="active"):
        try:
            seen = _dt.datetime.fromisoformat(task["updated_at"])
        except (ValueError, KeyError, TypeError):
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=_dt.timezone.utc)
        if (now - seen).total_seconds() < stall_after:
            continue
        if pass_blocked_by(task, spontaneous=True) is not None:
            continue          # a worker or a child IS running; quiet is not stalled
        stalled.append(task["id"])
    return stalled


def find_abandoned(stall_after: int = STALL_AFTER_SECONDS) -> list:
    """Active tasks whose thread says a worker is running, but whose worker is
    gone and will never report back.

    The stall watchdog deliberately skips anything with a worker in flight, so
    a worker that dies without reporting leaves a task that looks identical to
    one that is merely busy — and stays that way forever, since every pass is a
    no-op while the marker stands. Only a daemon restart clears it, via
    reconcile_on_boot. This is not hypothetical: cancelling a task by killing
    the subprocess (the only way to cancel today) produces exactly this state.

    A live worker holds a session; a dead one does not. That is what separates
    "quietly running a 15-minute Gradle build" from "killed" — the elapsed-time
    check alone cannot, and would abandon the slow build.
    """
    import datetime as _dt
    from . import sessions
    now = _dt.datetime.now(_dt.timezone.utc)
    abandoned = []
    for task in store.list_tasks(status="active"):
        if not _worker_in_flight(task["id"]):
            continue
        if sessions.list_for_task(task["id"]):
            continue          # a session backs it — the worker is alive
        try:
            seen = _dt.datetime.fromisoformat(task["updated_at"])
        except (ValueError, KeyError, TypeError):
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=_dt.timezone.utc)
        if (now - seen).total_seconds() < stall_after:
            continue          # may simply not have minted its session yet
        abandoned.append(task["id"])
    return abandoned


def rescue_stalled(stall_after: int = STALL_AFTER_SECONDS) -> int:
    """Queue a `continue` for every stalled task, and close the marker on any
    task whose worker died without reporting. Logs at WARNING: either case
    means work was lost somewhere, and that is worth seeing rather than
    silently papering over."""
    ids = find_stalled(stall_after)
    for task_id in ids:
        logger.warning(
            "watchdog: %s has been active with no worker and no activity for "
            "%ds — queueing `continue`. Some path created it without emitting.",
            task_id, stall_after,
        )
        emit(task_id, "continue")

    for task_id in find_abandoned(stall_after):
        logger.warning(
            "watchdog: %s is marked worker-in-flight but has no session and has "
            "been silent for %ds — its worker died without reporting. Closing "
            "the marker so the task can move.",
            task_id, stall_after,
        )
        store.post_message(
            task_id, "system", kind="worker_finished",
            text="worker vanished without reporting (no live session) — resuming",
        )
        emit(task_id, "worker_finished")
        ids.append(task_id)
    return len(ids)


async def watchdog_forever(interval: int = WATCHDOG_INTERVAL_SECONDS,
                           stall_after: int = STALL_AFTER_SECONDS) -> None:
    heartbeat.register("watchdog", every=interval, note="guarding stalled tasks")
    while True:
        await asyncio.sleep(interval)
        try:
            rescued = rescue_stalled(stall_after)
            heartbeat.beat("watchdog",
                           note=(f"rescued {rescued}" if rescued else "nothing stalled"))
        except Exception:
            # a watchdog that dies on one bad row stops guarding everything
            logger.warning("watchdog pass failed", exc_info=True)
