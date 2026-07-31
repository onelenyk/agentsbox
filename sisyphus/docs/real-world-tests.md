# Real-world test plan — 10 end-to-end scenarios

Each is a real task run through the live daemon (orchestrator + opencode agents),
not a unit test. They exercise the capabilities we built and, like the sandbox
run that found the `__pycache__` leak and the earlier one that found the
`hephaestus` stall, are meant to **surface real bugs**.

**Harness for every test:** register/prepare the repo → `POST /api/v1/tasks` →
poll to terminal → assert the thread shape + the on-disk result. Sandbox repos
live under `agentsbox/` so merges are safe to actually perform.

**Coverage map:** feature · refactor · bugfix · review-loop · repo-less · solo ·
fan-out · MR-conflict · branch-target · human-in-loop.

---

### 1. Multi-file feature with error handling
- **Repo:** testrepo
- **Goal:** "Add `multiply(a, b)` and `divide(a, b)` to calc.py; divide must raise
  ValueError on division by zero. Add tests covering both, including the zero
  case. Commit and open a merge request."
- **Exercises:** engineer multi-file edit, exception handling, test authoring.
- **Expect:** done; MR with calc.py + test_calc.py changes; reviewer APPROVED.
- **Verify:** merge into a throwaway branch → `pytest` green; a `pytest.raises`
  test exists for the zero case.

### 2. Modify an existing function (not just add)
- **Repo:** testrepo
- **Goal:** "Change `add` to accept any number of arguments (`add(*nums)`) and
  return their sum, keeping `add(2, 3) == 5` working. Update the tests. Commit,
  open an MR."
- **Exercises:** editing existing lines (diff churn on real code), backward
  compatibility, reviewer scrutiny of a behavioural change.
- **Expect:** done; diff touches existing `add`; old test still passes.
- **Verify:** merge → `pytest`; `add(1,2,3,4) == 10` and `add(2,3) == 5`.

### 3. Bug fix from a failing test
- **Repo:** testrepo-bug (seeded: a `factorial` with an off-by-one, and a
  **failing** test that encodes the correct behaviour).
- **Goal:** "The test suite is failing. Diagnose and fix the bug so all tests
  pass. Commit and open an MR."
- **Exercises:** explorer diagnoses → engineer fixes; the agent must *run* the
  tests to know it's fixed.
- **Expect:** done; the fix is minimal (one function); reviewer APPROVED.
- **Verify:** `pytest` was red before, green after the merge.

### 4. Review loop — request_changes → fix → approve
- **Repo:** testrepo
- **Goal:** "Add a `power(base, exp)` function to calc.py. It MUST have a
  docstring and type hints, and a test. Commit and open an MR." (An explicit
  quality bar the first attempt may miss.)
- **Exercises:** the reviewer's `request_changes`, the engineer re-spawn, the
  re-review, then `done`.
- **Expect:** thread ideally shows a CHANGES REQUESTED before an APPROVED (if the
  first pass is clean, still valid — note it).
- **Verify:** final `power` has a docstring + type hints; `pytest` green.

### 5. Repo-less research / planning
- **Repo:** none
- **Goal:** "Research and recommend: should the thread store move from SQLite to
  Postgres? Give 3 pros, 3 cons, and a one-line recommendation."
- **Exercises:** repo-less scratch dir, planner/explorer, no commit, no MR.
- **Expect:** done with a `result`; no `commit`/`mr` messages; no MR record.
- **Verify:** the result names a recommendation; disk has a scratch dir, no bare
  clone.

### 6. Single-agent (solo) mode
- **Repo:** testrepo, **agent pinned:** `engineer`
- **Goal:** "Add an `abs_diff(a, b)` returning `abs(a - b)` with a test."
- **Exercises:** the single-agent path (orchestrator skipped), one-shot completion.
- **Expect:** done; **no** orchestrator decision messages on the thread — just the
  pinned engineer's worker_started → result.
- **Verify:** thread has no `[spawn_worker]` orchestrator notes; the change exists.

### 7. Multi-repo fan-out
- **Repos:** testrepo + testrepo2 (a second sandbox), **parent:** repo-less
- **Goal:** "Add a `VERSION` constant set to '1.0.0' to both the iOS-style repo
  (testrepo) and the Android-style repo (testrepo2). One subtask per repo."
- **Exercises:** planner `propose_subtasks` → orchestrator `spawn_task` → two
  child tasks → child_finished roll-up → parent done when all children done.
- **Expect:** parent done; two children, each with its own commit + MR.
- **Verify:** board shows the parent with a `2/2` chip; each child touched its own
  repo; parent completed without a separate reviewer.

### 8. Local MR with a conflict
- **Repo:** testrepo, **target branch:** `feature` (pre-seeded to change the SAME
  lines of calc.py differently)
- **Goal:** "Rewrite `add` to log its arguments before returning. Commit, open an
  MR." (Then in the MR view, select target `feature`.)
- **Exercises:** the mergeability dry-run against a conflicting target.
- **Expect:** MR opens; mergeability shows **🔴 conflicts** for `feature`; the
  Merge button is disabled; **🟢** for `main`.
- **Verify:** `check_mergeability(...,'feature')` returns conflicts; the real repo
  is untouched.

### 9. Merge into a non-default branch (target selection)
- **Repo:** testrepo (has `main` + `develop`)
- **Goal:** "Add a `clamp(x, lo, hi)` function with a test."
- **Exercises:** after done, open the MR, **select `develop`**, Merge — the
  non-checked-out-target path (merge via a throwaway worktree).
- **Expect:** merged into `develop`; `main`'s working tree untouched.
- **Verify:** `develop` contains `clamp`; `main` does not; `pytest` green on
  `develop`.

### 10. Ambiguous request → ask_human → resume
- **Repo:** testrepo
- **Goal:** "Make the calculator better." (Deliberately underspecified.)
- **Exercises:** the orchestrator's `ask_human` (parks as `waiting`, doesn't
  loop), then a human reply resumes it.
- **Expect:** status `waiting` with a clarifying question; after `POST
  .../messages` with a concrete spec, it resumes and completes.
- **Verify:** a `question` message exists; the task didn't burn the pass budget;
  it finishes after the reply.

---

## Running & scoring

- Runs are ~3–8 min each (multiple opencode sessions); run in the background and
  batch. Full suite ≈ 40–70 min of agent time + token cost.
- Score each: **PASS** / **PASS-with-notes** / **BUG** (+ what surfaced). Any BUG
  gets a fix + a regression unit test, same as the two we already found.
- Teardown: delete the demo tasks; sandbox repos stay under `agentsbox/` unless
  told otherwise.

## Setup checklist (before running)
- `testrepo` — exists (calc.py + test, main/develop).
- `testrepo2` — create (mirror of testrepo) for #7.
- `testrepo-bug` — create with a seeded failing test for #3.
- `testrepo`'s `feature` branch — create with a conflicting calc.py for #8.

---

## Results — first full run (2026-07-31)

Model: `deepseek/deepseek-v4-flash` (cheap) for all agents. Total cost across all
10 runs ≈ **$1.40**. Unit suite: **350 green**.

| # | Scenario | Result |
|---|----------|--------|
| 1 | Multi-file feature (multiply/divide) | ✅ PASS |
| 2 | Refactor `add(*nums)` | ✅ PASS |
| 3 | Bug fix (factorial) — merged → pytest green | ✅ PASS |
| 4 | Review-loop (`power` + docstring/types) | ✅ PASS (first pass clean) |
| 5 | Repo-less research | ✅ PASS (+ real CHANGES_REQUESTED → APPROVED) |
| 6 | Single-agent (solo) | ✅ PASS (no orchestrator/reviewer) |
| 7 | Multi-repo fan-out | ✅ PASS (2 children, roll-up) |
| 8 | MR conflict | ✅ PASS (🔴 vs `feature`, 🟢 vs `main`) |
| 9 | Merge into `develop` (target select) | ✅ PASS (main untouched, pytest green) |
| 10 | Ambiguous request | ✅ PASS-with-note: **interpreted** (Calculator class, CLI, 47 tests) rather than ask_human |

### Bugs found & fixed (with regression tests)
1. **Hallucinated agent name** — the decider called `decide_spawn_worker(agent="code")`.
   Fix: the tool validates against the real roster and returns an error listing
   valid names, so the orchestrator re-picks in the same session.
2. **No decision produced** — the decider sometimes ended its turn without calling
   a tool. Fix: `spawn_decider` retries (`DECIDER_ATTEMPTS=2`) before ask_human.
3. **(earlier)** build junk (`__pycache__/*.pyc`) swept into commits — excluded.

### Notes
- v4-flash is reliable enough for **workers**; the **orchestrator** needed the
  decider hardening above (without it, 2/10 parked as `waiting`).
- The robustness guards (safe-spawn, ask_human) meant **zero stalls** even when
  the weak model misbehaved.
- The full local-MR flow was verified on **real merges**: current branch (#3),
  non-current branch via worktree (#9), and conflict detection (#8).
