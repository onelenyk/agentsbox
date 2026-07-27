# Bucket B — Roadmap: from "looks autonomous" to "actually ships"

Bucket A (commit `dc861db`) hardened the orchestrator loop against silent
stalls, duplicate workers, self-certified `done`, disk leaks, and un-deletable
tasks. **Bucket B builds the real capabilities** the autonomous-SDLC vision
needs. Each item below is a gap from the gap review, with goal, approach, key
files, effort, and dependencies.

The keystone is **#1 (real commits + MRs)** — several other items are only
meaningful once a task produces a real branch/MR.

## Sequencing (dependency-ordered)

1. **Real commits & merge requests** — the keystone
2. **Attachment persistence** — small, independent, immediately useful
3. **Repo-less tasks** — small, independent
4. **Structured, actionable result** — depends on #1
5. **Thread summarization / context windowing** — independent, bites on long tasks
6. **MR lifecycle / external events** — depends on #1 + #4
7. **API auth** — independent, optional for localhost

Recommended order: **1 → 2 → 3 → 4 → 5 → 6 → 7**.

---

## 1. Real commits & merge requests  *(keystone · Large)*

**Gap.** `web/agent_mcp.py::_commit` / `_open_mr` only post messages to the
thread — no real git commit, no branch, no push, no PR/MR. The engineer role
can "finish" work that produces zero real artifacts. This is the stated #1
roadmap goal.

**Goal.** The engineer's `commit` runs a real `git commit` on a task branch in
its worktree; `open_mr` publishes the change through a per-platform provider:
GitHub, GitLab, or a local repo with no forge. Credentials stay daemon-side —
the worker only calls the tool; the daemon executes with server-side creds.

### Settled design (decisions taken)

**Shared git spine (all platforms, in a new `core/git_ops.py`):**
- Create a task branch `sisyphus/task-<id>` in the worktree at spawn
  (`core/worktree.py`), based off the repo's configured default branch.
- `commit(worktree, message)` → real `git add -A && git commit` on that branch,
  returns the SHA. Platform-agnostic.
- Only `open_mr` differs → it delegates to a **provider** chosen by the repo's
  platform.

**Provider abstraction (`core/providers/`), one interface:**
`publish(worktree, repo, branch, title, body) -> ChangeRef`

- **GitHubProvider** — push branch to origin, `POST /repos/{owner}/{repo}/pulls`
  via REST with a token, return the PR URL. *(Auth: REST + token — decided.)*
- **GitLabProvider** — push branch, open an MR via REST with the token already
  in `config.yaml`, return the MR URL.
- **LocalProvider** *(source is a local path, no forge)* — compute
  `git diff <base>..<task-branch>`, store it as a viewable/downloadable patch,
  and post it to the thread as the change. **Read-only to the user's repo —
  nothing is pushed back** *(decided: diff-in-thread default; no push-back)*.

**Platform detection.** Auto-detect from the repo's `source` (`github.com` →
github; the configured GitLab host → gitlab; a filesystem path / `file://` →
local), with the per-repo `platform` field as an override. NOTE: `add_repo`
already accepts `platform` but `_save_repo_config` currently **drops it** — fix
it to persist, and derive the API project path (`owner/repo` for GitHub, project
path for GitLab) from `source`.

**Config.** Add a `github: {base_url?, token}` block alongside the existing
`gitlab: {base_url, token}` (base_url optional, for GitHub Enterprise).

**Tool surface stays identical** — engineer still calls `commit` then
`open_mr`; the `open_mr`-requires-`commit` precondition stays. The thread result
gets richer: a PR/MR **URL** for forge repos, or a **diff + branch name** for
local ones (this is what feeds roadmap item #4).

**Key files.** `web/agent_mcp.py` (rewire `_commit`/`_open_mr`), new
`core/git_ops.py`, new `core/providers/` (base + github + gitlab + local),
`core/worktree.py` (task branch), `core/repos.py` (persist `platform`,
source→project mapping), `core/config.py` / `config.yaml` (add `github` creds).

**Risks.** GitHub vs GitLab REST shape differences; push permissions; clone-URL
→ API-project mapping; `cleanup_task` must not reclaim the worktree before the
diff is captured / branch pushed (both happen in `open_mr`, before any terminal
status — safe).

**Sub-steps:** (a) task branch + real commit; (b) provider interface + local
diff; (c) GitLab REST publish; (d) GitHub REST publish; (e) surface SHA + URL/diff
on the thread.

---

## 2. Attachment persistence + viewing  *(Small-Medium)*

**Gap.** `attach(filename)` records a name, but the file lives in the worktree
(reclaimed on terminal) and the view route was removed — the human sees a
filename with no way to open it.

**Goal.** Attached files are persisted and viewable in the task detail UI.

**Approach.** On `attach`, the daemon copies the named file from the session's
worktree into a durable per-task store (`workspace/tasks/<id>/attachments/`) at
call time — *before* any cleanup. Add `GET /api/v1/tasks/<id>/attachments/<name>`
to serve it and render attachment messages as links. `cleanup_task` removes
worktrees/runs but **not** the attachments store (or is extended to remove it
only on task delete, not on done — decide: keep attachments for done tasks).

**Key files.** `web/agent_mcp.py::_attach`, `web/api.py` (+ serve route),
`web/routes.py` / `task_detail_content.html` (link), `core/worker.py::cleanup_task`.

---

## 3. Repo-less tasks  *(Small-new)*

**Gap.** Tasks without a repo are rejected (Bucket A guard) — but
research/planning/Q&A tasks legitimately need no checkout.

**Goal.** Allow a repo-less task to run in a private scratch dir.

**Approach.** Give repo-less tasks a per-task scratch dir
(`workspace/scratch/<id>/`), always write `opencode.json` there, and drop the
repo-required guard for those. Restrict repo-less tasks to the `explorer` role
(no `commit`/`open_mr` without a repo), or have those tools return a clear
"needs a repo" error. Extend `cleanup_task` to the scratch dir.

**Key files.** `core/worker.py` (scratch-dir path), `web/api.py` +
`web/routes.py` (relax the guard), `core/roles.py` (optional).

---

## 4. Structured, actionable result  *(Medium · depends on #1)*

**Gap.** A finished task's result is free-text prose — no link to a
branch/commit/MR/diff, nothing to click through to.

**Goal.** A task carries a structured result (summary + links to branch, MR,
attachments) surfaced as a "Result" card on the detail page.

**Approach.** Extend `finish`/`approve` to capture optional structured fields
(`mr_url`, `branch`); render a Result card that pulls the latest `result`
message plus any `mr`/`commit` messages. Keep it thin — no new table, just
richer message payloads + a focused UI block.

**Key files.** `web/agent_mcp.py` (finish/approve args), `task_detail_content.html`.

---

## 5. Thread summarization / context windowing  *(Medium)*

**Gap.** Both the orchestrator decision call and every worker prompt embed the
*entire* thread → unbounded tokens and eventual context-window overflow on a
long task.

**Goal.** Bound the tokens fed to prompts without losing the plot.

**Approach.** When a thread exceeds N messages/chars, `render_thread` gains a
bounded "for prompt" mode: goal + a rolling summary of older messages (one
cheap model call, cached as a special `summary` message) + the last K verbatim.

**Key files.** `core/store.py::render_thread`, `core/orchestrator.py` and
`core/worker.py` (call the bounded render).

**Risk.** Summary quality — keep the verbatim tail generous; only summarize the
old head.

---

## 6. MR lifecycle / external events  *(Large · depends on #1, #4)*

**Gap.** No reaction to what happens to an MR after it's opened — CI results,
review comments, merge. Only 3 internal event types exist.

**Goal.** An external event (CI failed, review comment, MR merged) wakes the
orchestrator for the right task.

**Approach.** A webhook receiver (`POST /api/v1/webhooks/<platform>`) maps an
incoming platform event to a task via the stored MR URL → task link (from #1/#4)
and `emit`s a new `external` event. The orchestrator prompt learns to handle it
(CI failed → spawn engineer to fix; approved+merged → done). For local/no-public
setups, a polling fallback.

**Key files.** new `web/webhooks.py`, `core/orchestrator.py` (new event type +
prompt), a task↔MR index.

---

## 7. API auth  *(Small-new · optional)*

**Gap.** `/api/v1` and the htmx routes have no auth (the agent-MCP has a session
token; the rest is open on the bound host).

**Goal.** Protect the daemon's control surface with a shared token.

**Approach.** A bearer-token check (token from config/env); the browser UI and
`mcp_server.py` send it. Skippable for pure-localhost by config.

**Key files.** `web/app.py` (middleware), `mcp_server.py` + `web/routes.py`
(send token), `core/config.py`.

---

## Not in Bucket B (deferred / by design)

- **Cancel a *running* task** (kill the live subprocess) — needs tracking the
  running worker handle; small-new, do alongside #1 if worktree writes become
  expensive to abandon.
- **Multi-worker parallelism per task** — the loop is deliberately one action
  per event; revisit only if serial throughput becomes a real limit.
