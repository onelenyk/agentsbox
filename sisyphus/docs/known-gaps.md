# Known gaps

Things verified to be missing or wrong, with enough detail to act on. Each was
found by using the system, not by reading it. Remove an entry when it is fixed —
this file is only useful if it is true.

Last reviewed: 2026-08-20.

Closed since the last review, kept only as a pointer to where the guard now
lives: Stop racing a not-yet-started worker, and the subprocess kill having no
automated test — both af6fe05, guarded by `tests/test_task_lifecycle.py`
(`TestStopBeforeTheWorkerStarts`) and `tests/test_launcher_cancel.py`.

Closed 2026-08-17..20:

- *An approval can rest on a number nobody read* — the daemon now runs the
  repo's own `check:` command against the branch and records the verdict on the
  MR before the reviewer is asked anything (`core/verify.py`, fd2f86c). "No
  check configured" is recorded as `not_run` and can never read as a pass.
- *Agents compare against the wrong baseline* — the merge base is computed and
  handed over as a SHA instead of described as a rule
  (`mergerequests.diff_range`, `get_mr_diff`, 9dcfc36).
- *Untracked local config never reaches a worker* — a repo may declare a
  `setup:` block that copies files into the worktree or runs a command
  (`core/setup_hook.py`, 8211edd).

---

## Task lifecycle

### There is no "retry from scratch"
**Status:** CLOSED 2026-08-21 — `POST /api/tasks/{id}/retry` (merged as 7e5a160).
Reopen resumes a task on its existing thread and branch. Retry-from-scratch is
its complement: a done/failed task gets a "Retry from scratch" action that
creates a brand-new task with the same goal/repo/agent — fresh id, fresh branch,
clean thread — cross-linked to the original by thread notes. Useful when the
thread itself is what went wrong (a bad early decision the model keeps
re-reading and repeating).

---

## Review loop

### A task reviewer's verdict never reaches the MR row
**Status:** open, found 2026-08-21.
When a task's own reviewer writes APPROVED in the thread, `mr.review_status`
stays `pending` — only the mr-reviewer autopilot's verdict path calls
`set_review_status`. Observed on mr-9b6852f8: the reviewer approved in the
thread, and the review-gated merge then (correctly, per its own record) refused
the human's merge until the verdict was recorded by hand. The gate did its job;
the recording path is what's missing — the orchestrator's approval handling
should set the MR's review_status the same way the autopilot does.

### An approval can rest on a number nobody read
**Status:** CLOSED 2026-08-18 — see the header. Kept for the reasoning.
Reviewer personas now require quoting the command and the last line of its
output, and forbid approving with any failure. But nothing *checks* the quote:
a reviewer once reported "604 passed, 3 pre-existing failures (unrelated)" on a
branch that was clean, and approved on the strength of dismissing failures that
did not exist. The persona makes lying harder, not impossible.

Real fix: have the daemon run the suite itself and attach the result to the MR,
so the verdict cites a number the system produced rather than one the agent
typed.

### Agents compare against the wrong baseline
**Status:** CLOSED 2026-08-18 — the baseline is now computed, not prompted.
Both the engineer and the reviewer checked file modes with `git ls-tree HEAD~1`
— the previous commit on their own branch — instead of the branch base, and each
concluded the modes were clean. Three scripts had in fact gone 755 → 644 in an
earlier commit, which would have broken `fastlane/lokalise_pull_android.sh`.
Each ran a real command and drew a false conclusion from it.

All three agent personas now state the rule: the baseline is
`git merge-base HEAD <target-branch>`, never HEAD~1. That is a prompt, not an
enforcement — nothing checks which command an agent actually ran, so treat this
as reduced likelihood rather than a fix. It has not yet been observed working on
a real MR.

---

## Environment

### Untracked local config never reaches a worker
**Status:** CLOSED 2026-08-18 — a per-repo `setup:` hook. Kept for the reasoning.
Workers get a fresh clone, so anything gitignored is absent — `local.properties`,
`.env`, credentials. `worker_env` in `config.yaml` covers environment variables
(that is how JAVA_HOME/ANDROID_HOME got there), and Android happens to accept
`ANDROID_HOME` instead of `local.properties`, so this has not bitten yet. A
project that genuinely needs a *file* present has no answer today.

**Likely fix:** a per-repo `setup` hook — files to copy into the worktree after
clone, or a command to run.

---

## Product surface

### A reply to a blocked task is wiped while you type it
**Status:** CLOSED 2026-08-20 — the reply input now carries `hx-preserve` with a
task-derived id (`reply-text-<task id>`), so htmx keeps the live element across
every `#inbox` swap — the 10s poll and the `taskUpdated`/`inboxChanged` events
alike — protecting typed-but-unsent text, focus and cursor.

### Classify gives no sign that it is working
**Status:** CLOSED 2026-08-21 — the classify form and the command result's action
buttons now disable themselves and show a "Classifying…"/"Working…" hint
(htmx `hx-indicator` + `hx-disabled-elt`) while their request is in flight.
It is a model call taking several seconds and the page shows nothing until it
returns, so it reads as a dead button. I assumed my own click had missed and
went to the console to check.

---

## Housekeeping

### Reclaiming a task's disk was every caller's job to remember
**Status:** CLOSED 2026-08-20.
`cleanup_task` keeps the per-task bare clone while an MR is open and runs when
the *task* reaches a terminal status — always before a human merges. Four call
sites each remembered to reclaim afterwards (three MR routes and the planner's
auto-merge), which worked; what did not was a fifth path. Merging
programmatically through `mergerequests.merge_branch` + `mark_merged` — as a
script or another tool would — skipped all four, and four finished tasks had
left 26MB of bare clones and two worktrees behind.

Reclaiming is now a property of the state change (`reclaim_if_settled`, called
from `mark_merged` and from `set_status` on merged/closed), and the four
explicit calls were removed. It does nothing while another MR for the task is
still open, and a failure to reclaim can never fail a merge.
