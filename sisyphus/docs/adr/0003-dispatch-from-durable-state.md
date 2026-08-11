# ADR 0003 — Dispatch from durable state, not from a queue

**Status:** Proposed.
**Date:** 2026-08-11
**Extends:** [ADR 0002](0002-task-thread-orchestration.md) — does not supersede
it. The two-entity model and the cold-function orchestrator stand; this fixes
how a pass gets *triggered*.

## The claim being corrected

ADR 0002 says state lives entirely in the append-only thread, so a daemon
restart just resumes. That is not quite true today, and the gap has cost real
time.

A pass only happens because someone called `emit(task_id, type)`, and the queue
that call feeds is `asyncio.Queue` in process memory (`core/orchestrator.py`).
The thread is durable; the *intention to act on it* is not. So the real
invariant is not "state lives in the thread" but "state lives in the thread
**and** somebody remembered to enqueue an event, **and** the process has not
restarted since".

`reconcile_on_boot()` hides this by reconstructing the queue from the thread at
startup. That it can do so is the point: **if the queue can be rebuilt by
scanning durable state, then the queue was never authoritative — it was a cache
of a predicate.** A cache that nothing keeps coherent.

## Evidence, not theory

Three separate hand-rolled compensations for one missing invariant already
exist in the codebase:

1. `reconcile_on_boot()` — rebuilds the queue after a restart.
2. `handle_event()` re-emits `continue` after a `note`/`noop` decision, with the
   comment *"never a silent stall"*.
3. `watchdog_forever()` — added 2026-08-11, scans for active tasks with nothing
   queued.

Each was written at a different time, by noticing a different instance of the
same hole. And the hole still bit twice this week:

- **task-246eb605** sat `active` with an empty thread for **2h13m**. `open_mr`
  created the review task and never emitted. It moved only because an unrelated
  daemon restart happened to run `reconcile_on_boot`. Without that restart it
  would have waited indefinitely.
- Adding the missing `emit` at that one call site fixed that one call site. It
  established no invariant. The next caller can forget exactly as easily.

There is also a telling asymmetry in how the two vocabularies are defended:

| | declared as | enforced by |
|---|---|---|
| `Action` | `Literal["spawn_worker", …]` | pydantic — an invalid action fails the task loudly |
| `Event.type` | bare `str` | a comment listing the values |

The action space — the thing an LLM chooses from — is rigorously bounded. The
event space — the thing our own code chooses from — is a convention.

## The decision

**A pass is owed when a predicate over durable state says so. `emit` becomes a
latency hint, not a correctness requirement.**

The predicate is not new logic. It was already written twice, inline in
`handle_event` and again in `find_stalled` — and the copies had drifted:
`find_stalled` omitted the pending-children check, so a parent mid-fan-out could
be reported as stalled and logged as a fault.

Phase 2 extracted it as `pass_blocked_by(task, spontaneous=False)`, returning the
reason a pass is *not* owed, or `None` if it is.

**The `spontaneous` flag is load-bearing, not cosmetic.** Writing this ADR I
first stated the predicate as a single condition including `status == "active"`,
and implementing it that way broke a path immediately: `task_reply` emits
`human_replied` *without* setting the status back to `active`
(`web/routes.py`), so a parked task arrives at `handle_event` still `waiting`.
A predicate that requires `active` silently kills every
human-answers-a-question resume. The two callers need genuinely different
questions:

- **event-driven** (`spontaneous=False`) — an event brought us here, so a
  `waiting` task may proceed. That *is* the resume.
- **scan-driven** (`spontaneous=True`) — parked means parked; nothing may wake a
  `waiting` task of its own accord.

Both cases now have a test, because the distinction is invisible in the code and
expensive to rediscover.

Check it against every way a pass can end, and note that no fourth clause is
required — the existing guards already partition the space:

| last decision | why no pass is owed |
|---|---|
| `spawn_worker` | worker in flight |
| `spawn_task` | children pending |
| `ask_human` | status is `waiting` |
| `done` / `fail` | status is terminal |
| `note` / `noop` | **a pass IS owed** — which is exactly why the code re-emits by hand today |
| *(task just created)* | **a pass IS owed** — the 2h13m bug |

The two rows where the predicate returns true are precisely the two cases the
codebase currently patches by hand. That is the whole argument: the compensations
are the predicate, discovered piecemeal.

Runaway is bounded as it is today, by `MAX_PASSES` (25) and
`MAX_CONSECUTIVE_REPEATS` (3) — a `note → note → note` loop trips the same
breaker whether the wake-up came from an `emit` or from a scan.

## Migration

Each phase is independently shippable and leaves the system working.

**Phase 1 — done (`d2c5021`).** The watchdog. A coarse version of the predicate
on a 2-minute tick with a 10-minute staleness threshold. The threshold exists
only to avoid racing a task between `emit` and the pass starting; it is a proxy
for "not already dispatched" and Phase 3 removes the need for it.

**Phase 2 — one predicate, no behaviour change.** Extract `needs_pass()` and
have both `handle_event`'s inline guards and `find_stalled` call it. Pure
refactor, provably equivalent, and it makes the rule reviewable in one place.

**Phase 3 — scan-driven dispatch.** The loop wakes on an event *or* a tick, then
dispatches every task where `needs_pass()` holds. `emit` still fires and still
gives sub-second latency — it just stops being load-bearing.

This phase carries the one genuinely hard problem, and it must not be waved
through: **there is a window in which a worker has been launched but is not yet
observable in the thread.** `_execute` calls `asyncio.create_task(...)` and
returns; `_spawn_worker_safe` posts `worker_queued` as its first statement, but
that statement has not run yet. `asyncio.Queue.get()` takes a fast path when the
queue is non-empty and need not yield, so a second pass can begin before the
marker lands. This is not hypothetical — the comment at
`core/orchestrator.py:493` records the live failure: *8 phantom spawns while two
other tasks held both slots*. The `worker_queued` marker was the fix, and it
narrows the window rather than closing it.

A time threshold (what the watchdog uses today) papers over this by refusing to
look at anything younger than 10 minutes. Scan-driven dispatch cannot afford
that, so it needs the window closed properly: hold the background handle
returned by `_execute` in `task_id -> handle`, and have `needs_pass()` return
false while a live handle exists. The handle exists from the instant
`create_task` returns, so there is no gap for a second pass to slip through, and
it is a fact about the process rather than a guess about elapsed time. The
thread marker stays as the cross-restart answer — a handle does not survive a
restart, but after a restart there is no in-flight worker to protect against.

Only once that holds can the staleness threshold be deleted.

**Phase 4 — drop `event_type` from the decision path.** Two things depend on it,
not one, and the second was missed on the first writing of this ADR:

1. `handle_event` bypasses the budget guard when the type is `human_replied`.
2. More importantly, the *existence* of that event is the only thing that
   resumes a `waiting` task. A scan will never wake one, by design.

Both are recoverable from the thread: "the last message is authored by `human`
and the orchestrator has not acted since" answers the budget bypass and the
resume together, and it is durable, where the event is not. That check has to
land before the event vocabulary can go — otherwise the first parked task after
a daemon restart never resumes, which is a worse bug than the one this ADR
fixes.

**Phase 5 — retire the vocabulary.** With nothing branching on it, `Event`
collapses to a task id, or disappears. `reconcile_on_boot` becomes redundant:
restart is a no-op because the predicate is recomputed from durable state on the
next tick, which is what ADR 0002 claimed all along.

## Consequences

**Good.**

- Correctness stops depending on any caller remembering to emit. The class of
  bug closes, rather than each instance being patched.
- ADR 0002's central claim becomes literally true.
- `reconcile_on_boot`, the `note`/`noop` re-emit, and the watchdog collapse into
  one rule instead of three compensations that must each be maintained.
- A stalled task is now a *bug in one predicate* — findable and testable —
  instead of an absence distributed across every call site.

**Costs, stated honestly.**

- Every wake-up scans active tasks, and `_worker_in_flight` / `_children_pending`
  each read a full thread. That is O(active × thread length) per tick. At
  current scale (0–3 active tasks) it is free. If it ever isn't, the fix is a
  watermark column, not a return to the queue.
- **A bug in `needs_pass()` now affects every task, where a missing `emit`
  affected one.** This is a real trade: a distributed failure becomes a central
  one. It is worth it because a central failure is *visible and testable* while
  a distributed one is silent — but it means the predicate needs adversarial
  tests, especially the false positive that matters: a worker that is merely
  quiet must never be treated as stalled. That test exists
  (`test_does_not_touch_a_task_whose_worker_is_running`) and must not be deleted.
- Sub-second latency now depends on `emit` still being called, so removing an
  emit degrades responsiveness silently. Phase 3 should log when a task is
  picked up by scan rather than by event — a rising count means someone dropped
  an emit, and the system should say so rather than quietly absorbing it.
- Phase 3 replaces a time threshold with a handle registry, which is more
  precise but is also live process state — the very kind of thing this ADR
  argues against relying on. The distinction that makes it acceptable: the
  registry only ever *suppresses* a pass, and only for the lifetime of a
  process. Losing it on restart cannot strand a task; it can at worst permit one
  redundant pass, which the existing guards absorb. Authority still rests
  entirely in the thread.

## Scope — what this does not fix

This makes *when a decision happens* deterministic. It does nothing about *which
decision* — that is a model call, and the same thread can yield a different
action from the bounded set of seven. That non-determinism is deliberate and is
what the guards, the reviewer requirement, and the circuit breakers exist to
contain.

Nor does it address the second inconsistency class found this week: two objects
whose lifecycles are coupled only by convention, which produced an MR stuck at
`review_status = pending` while its review task read `done`. That was fixed by a
tool precondition (`finish` refuses on a review task) and deserves its own
treatment — the general question is which cross-object invariants should be
enforced at the write, and it is not answered here.
