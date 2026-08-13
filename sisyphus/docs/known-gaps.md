# Known gaps

Things verified to be missing or wrong, with enough detail to act on. Each was
found by using the system, not by reading it. Remove an entry when it is fixed —
this file is only useful if it is true.

Last reviewed: 2026-08-13.

Closed since the last review, kept only as a pointer to where the guard now
lives: Stop racing a not-yet-started worker, and the subprocess kill having no
automated test — both af6fe05, guarded by `tests/test_task_lifecycle.py`
(`TestStopBeforeTheWorkerStarts`) and `tests/test_launcher_cancel.py`.

---

## Task lifecycle

### There is no "retry from scratch"
**Status:** open, not started.
Reopen resumes a task on its existing thread and branch. There is no way to
re-run a task ignoring everything it did — useful when the thread itself is what
went wrong (a bad early decision the model keeps re-reading and repeating).

---

## Review loop

### An approval can rest on a number nobody read
**Status:** partly mitigated.
Reviewer personas now require quoting the command and the last line of its
output, and forbid approving with any failure. But nothing *checks* the quote:
a reviewer once reported "604 passed, 3 pre-existing failures (unrelated)" on a
branch that was clean, and approved on the strength of dismissing failures that
did not exist. The persona makes lying harder, not impossible.

Real fix: have the daemon run the suite itself and attach the result to the MR,
so the verdict cites a number the system produced rather than one the agent
typed.

### Agents compare against the wrong baseline
**Status:** mitigated 2026-08-13, unproven.
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
**Status:** open, worked around.
Workers get a fresh clone, so anything gitignored is absent — `local.properties`,
`.env`, credentials. `worker_env` in `config.yaml` covers environment variables
(that is how JAVA_HOME/ANDROID_HOME got there), and Android happens to accept
`ANDROID_HOME` instead of `local.properties`, so this has not bitten yet. A
project that genuinely needs a *file* present has no answer today.

**Likely fix:** a per-repo `setup` hook — files to copy into the worktree after
clone, or a command to run.
