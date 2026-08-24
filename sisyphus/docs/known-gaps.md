# Known gaps

Things verified to be missing or wrong, with enough detail to act on. Each was
found by using the system, not by reading it. Remove an entry when it is fixed —
this file is only useful if it is true.

Last reviewed: 2026-08-24.

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

## Worker runs

### An engineer that finished its work times out instead of finishing
**Status:** CLOSED 2026-08-23 — root cause was ours, not the runner's, and the
first diagnosis below was wrong. The run log shows the real chain: the open_mr
MCP handler ran the repo's check (150s+) INSIDE the request; the runner's MCP
client timed out at ~60s, dropped the server, the engineer retried open_mr 160
times against an empty roster and burned to the 1800s total timeout. Fixed
twice over: the check now runs off the request path (open_mr answers at once),
and a stalled run that already opened its MR ends with an explicit
"work is delivered — do NOT respawn an engineer" message for the decider.

### The suite a worker runs errors out on SISYPHUS_DB_READONLY
**Status:** CLOSED 2026-08-23 — replaced by SISYPHUS_DB_PROTECT=<path>: only
the daemon's own database is read-only; a suite's per-test tmp databases write
normally. Rehearsed under the exact worker env: full suite green. The blanket
flag still works where explicitly set.
Workers get SISYPHUS_DB_READONLY=1 (rightly — Seatbelt cannot block writes to
the daemon DB, the flag is the only guard). But it makes every SQLite
connection read-only including the per-test tmp databases, so an engineer who
obediently runs `python3 -m pytest -q` sees ~540 errors and has to diagnose
the env var (this one did, correctly, and shipped anyway). The MR check runs
unsandboxed with the flag unset and stays authoritative. Tension to resolve:
telling engineers to unset the flag would reopen the very hole it guards.
Candidates: persona note "the recorded MR check is the authoritative suite
run; treat DB_READONLY errors as environmental", or a test-only DB path
exemption in core/db.py (risky — needs care not to become a bypass).

### A worker's tool roster carries the user's whole desktop
**Status:** CLOSED 2026-08-24 — the worktree's own opencode.json now disables
every foreign MCP server by name (`core.runner_profiles.foreign_mcp_servers`).
Proven against the real binary: eight servers (websearch, context7, grep_app,
engram, macos-use, playwright, android, figma) resolve `enabled=False` inside a
generated worktree while ours stays enabled.

The names are asked for, not assumed: `opencode debug config` prints the
configuration that binary will actually load, probed from an empty directory so
what comes back is only what arrives from outside a project. Two things were
measured and rejected first — neither OPENCODE_CONFIG_DIR nor OPENCODE_CONFIG
suppresses the global entries (both merge), while a project-level
`{"enabled": false}` wins for local and remote servers alike. The stub is bare
on purpose: copying the original entry to flip a flag would write the user's own
URLs and environment, credentials included, into a file inside the worktree.
One trap worth keeping: the resolved config runs past 64KB and opencode exits
before a pipe drains, so `capture_output` returns it cut mid-string every time —
the probe writes to a file.

Open question, not a known leak: `debug config` still lists the user's plugins
(engram.ts, oh-my-openagent) even with `"plugin": []` and the DISABLE switches
set. The plugin hijack was fixed behaviourally in 8211edd/8638138, so this may
be resolution output rather than what loads. Nobody has re-checked it against a
live run's roster.

Earlier state, kept for the reasoning: the engineer's available-tools list
included figma, playwright, macos-use, engram, websearch and the oh-my-openagent
plugin suite — the user's GLOBAL opencode config merges into every worker run.
That is surface (an agent can drive the user's browser or Figma) and spend. The
env overlay sets OPENCODE_DISABLE_DEFAULT_PLUGINS / _EXTERNAL_SKILLS /
_CLAUDE_CODE_SKILLS (from the binary's own help text), which strips the plugin
and skill share. An empirical roster probe (invalid-tool trick) stalled —
opencode ran headless for 5 minutes with zero output — which is why this was
left open until a probe that costs a second rather than a run turned up.
claude-cli is already strict (--strict-mcp-config).

## Review loop

### Merging an MR reclaims worktrees out from under a live worker
**Status:** CLOSED 2026-08-22 — `reclaim_if_settled` now skips while `sessions`
reports an in-flight worker for the task; cleanup runs again at terminal
status, so nothing is lost. Mutation-checked.
`reclaim_if_settled` fires on `mark_merged`/`set_status` and calls
`cleanup_task` when the task has no open MR left — but it never asks whether a
worker is still running for that task. Observed on task-a5c0604b: the human
merged mr-7cdb06dc while the task's own reviewer was mid-run; cleanup salvaged
and removed both worktrees (see the `salvaged uncommitted work` warnings), and
the reviewer kept running against a directory that no longer exists, so its
`worker_finished` — the event the orchestrator sleeps on — was delayed until
the timeout path. Fix: `reclaim_if_settled` should skip (or defer) reclaim while
`sessions` reports an in-flight worker for the task, the same guard the reaper
already applies.

### Two orphan tests fail in a sandboxed worktree and tax every review
**Status:** CLOSED 2026-08-22, both halves. The two kill-path tests now probe
whether a killed child's death is even observable (Seatbelt blocks `ps`, so
`_zombie` cannot see the corpse) and skip with an honest reason when it is
not. And both reviewer personas now treat the MR's recorded automated check
as authoritative over their own sandboxed run — they stop re-running the
suite when a verdict is recorded, and never block on a local environmental
failure the record contradicts.
Reviewers run the suite inside their Seatbelt-sandboxed worktree, where two
process-inspection tests from tests/test_orphans.py fail for environmental
reasons (the sandbox constrains ps/sysctl-style process access — same family as
the long-known test_daemon_ctl::test_status_running). The daemon's own `check:`
runs unsandboxed and passes 1030, so every reviewer ends up asking a human to
adjudicate failures that have nothing to do with the diff. Fix candidates:
skip these tests when running sandboxed (detect the sandbox, `pytest.mark.skipif`),
or teach reviewer personas that the MR's recorded check verdict is authoritative
over their local run.

### A task reviewer's verdict never reaches the MR row
**Status:** CLOSED 2026-08-22 — approve/request_changes now also record the
verdict on the task's own open MR (a settled MR is left alone).
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
