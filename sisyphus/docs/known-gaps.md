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
**Status:** CLOSED 2026-08-24 — the MCP half. The plugin half has its own
entry below, with its cause. The worktree's own opencode.json disables every
foreign MCP server by name (`core.runner_profiles.foreign_mcp_servers`).
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

The plugin half of this is NOT closed and has its own entry below — the live
roster settled it.

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

### The global opencode plugin loads in every worker, and its tools come with it
**Status:** CLOSED 2026-08-24 — worker runs get a HOME of our own
(`core/runner_home.py`), and the roster went from 33 tools to 15.

Before (task-2ff103e9, a worker asked to name its own tools verbatim):

    ast_grep_replace ast_grep_search background_cancel background_output bash
    call_omo_agent edit glob grep interactive_bash look_at lsp_diagnostics
    lsp_find_references lsp_goto_definition lsp_prepare_rename lsp_rename
    lsp_symbols read session_info session_list session_read session_search
    sisyphus_agent_* skill skill_mcp todowrite webfetch write

After:

    bash edit glob grep read write webfetch todowrite skill task
    sisyphus_agent_attach sisyphus_agent_comment sisyphus_agent_get_context
    sisyphus_agent_finish sisyphus_agent_propose_subtasks

**HOME was the lever, and there wasn't a second one.** opencode resolves config,
cache, data and state from `$HOME` ahead of everything meant to move them. All
of these were measured and left the roster unchanged: project-level
`"plugin": []`, all three `OPENCODE_DISABLE_*` switches, `--pure` (it strips
only REMOTE MCP servers — and, correcting an older note, does NOT strip our own
project-local one), `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `XDG_CONFIG_HOME`
(which really does move the config dir — `debug paths` confirms — and still
changed nothing) and `XDG_CACHE_HOME` (which made it worse: a fresh install
registered `lsp` and `codegraph` on top).

**A wrong root cause, corrected the same day.** It looked like the `opencode` on
PATH — a Superset wrapper ending `export OPENCODE_CONFIG_DIR=<its own dir>` then
`exec` — was overwriting our environment. It is not: the runner row already
points at the real binary, so that wrapper never runs for a worker. The
explanation was tidy, fit every symptom, and was false. Kept here because the
next person will find the wrapper too.

**The home is an allowlist**, and that is the durable part: `.local/share/
opencode` (credentials — opencode keeps them in the DATA dir, apart from the
config that carries plugins, which is what makes this possible at all),
`.claude`/`.claude.json`, `.gitconfig`, `.ssh`. Nothing else arrives. A worker
that needs something new fails visibly and someone adds a line with a reason —
the opposite of a worker quietly carrying the whole machine.

Not a judgement on the plugin: the owner uses oh-my-opencode and wants it. It
still loads in their terminal. A Sisyphus worker is a different context, and
what reaches it is now a decision rather than a leak.

**How to re-measure in one run, without reading any source:** create a task
whose first instruction is "call comment() with the EXACT list of tool names you
can see, one per line, verbatim". The roster is the measurement. `opencode debug
config` is NOT sufficient for plugins — it reported the same list under every
configuration tried, including ones where the plugin demonstrably was not
loading.

### `daemon stop`/`restart` killed live workers without a word
**Status:** CLOSED 2026-08-24 — both refuse while a run is in flight, and name
what they would have killed:

    Not restarting: 2 worker run(s) in flight.
      engineer on task-97a05500 (13m in)
      mr-reviewer on task-be11f605 (2m in)
    Their work dies with the daemon and the run has to start over.
    Wait for them, stop the tasks in the UI, or pass --force.

Found by doing it: a restart to deploy a UI change killed a live engineer 72
seconds into its run (task-97a05500). The task survived only because the pinned
retry had landed that morning, then spent its second attempt from scratch and
timed out. Nothing anywhere had said the run was there.

The session registry lives in the daemon's memory and the CLI is a different
process, which is why this was never checked — so `/api/v1/health` now carries
`workers`, the one endpoint the CLI already calls without a token. A daemon that
is down or too old reports nothing: that is not proof the machine is idle, but
there is nothing we could name, and refusing to stop a daemon we cannot reach
would be worse.

### A working agent and a dead one looked exactly the same
**Status:** CLOSED 2026-08-24 — the task page now says when the runner last
produced anything: `no output yet` / `last output 4s ago` / `last output 3m ago`.

An agent writes to the thread only when it chooses to call `comment`. One run
posted "Understood: ..." and then said nothing for thirteen minutes while it
read code — and there was no way, from the page, to tell that from a corpse.
The role and the elapsed minutes were already shown; neither answers the
question anyone actually has.

The daemon knew all along. `launcher._read_stream` stamps `activity["ts"]` on
every chunk to enforce the idle timeout, and simply never told anyone. It now
also calls an `on_output` callback — throttled to once a second, so a chatty
runner cannot touch the session on every 64KB, and wrapped so that telling
nobody can never break the read. `sessions.note_output` stamps it on every live
session for that task.

Not solved by this: WHAT the agent is doing. That needs parsing the runner's
own stream format, which differs per CLI. "Alive or not" was the question being
asked.

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
