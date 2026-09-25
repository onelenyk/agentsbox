# Integrations and the journal

Sisyphus speaks to external systems — Jira for issue tracking, GitHub and GitLab for
forges. Every call to these systems leaves a journal entry, and the Integrations page
is a view of that journal.

## The journal

The journal (`integration_events` table) records every call to a tracker or forge.
One row per call: who acted (an agent, the daemon's loop, or a human), what was done,
against what, whether it worked. The Integrations page reads this table and nothing
else — it shows what happened, and a new provider added later needs no new page.

An actor is a string in a fixed shape:

```
agent:<role>@<task_id>       worker's tool call (engineer@task-1a2b3c4d)
daemon:<loop>                background loop (daemon:jira-poll)
human:ui                     UI button
human:cli                    CLI command (sisyphus.py)
```

Fields in a journal row:

- **provider**: jira | gitlab | github
- **action**: search | get | transition | comment | myself | …
- **actor**: the shape above
- **subject**: the thing the action was about (issue key, JQL, PR URL)
- **detail**: one sentence summary ("fetched 50 issues", "moved VAN-12 to In Progress")
- **count**: how many things (issues fetched, tasks created)
- **ok**: 1 if the action succeeded, 0 if it failed
- **error**: the provider's words when ok=0
- **task_id**: Sisyphus task involved, if any
- **meta**: JSON; action-specific data (retries, transitions, etc.)

## Configuring Jira

Open **Integrations → Keys & tokens** and fill in the Jira site URL, account
email and API token. That page is the only place Sisyphus reads credentials
from — not `.env`, not the shell, not a value typed into `config.yaml`.

Where they are kept: Sisyphus's vault, a keychain file of its own
(`~/Library/Keychains/sisyphus.keychain-db`) that is not on the user's
keychain search list and whose random password lives in the login keychain.
Every worker's sandbox denies any access to that file, so even `security
find-generic-password` from an agent's shell finds nothing. Values are never
put into a worker's environment; agents reach Jira only through the `jira_*`
tools.

`config.yaml` names them as placeholders, filled from the vault at load:

```yaml
jira:
  base_url: ${JIRA_BASE_URL}
  email: ${JIRA_EMAIL}
  token: ${JIRA_API_TOKEN}
```

The same page covers GitLab, GitHub, OpenRouter, the daemon's API token and
webhook secrets, and one forge token per repo (`REPO_<NAME>_TOKEN`, which wins
over the shared one). Changing a site URL or email clears the token that goes
with it, so a token is never sent to an address nobody re-confirmed. Anything
an older setup left in `.env` or typed into `config.yaml` is listed there —
**Move into the vault** moves it and removes it from the file.

The token is not your password. Generate one in Jira → Personal settings → API tokens.

Check that Jira is configured:

```sh
python3 sisyphus.py jira check
```

Or in code:

```python
from core import jira
result = jira.check()  # {ok: bool, detail: str}
```

## How issues become tasks

When you enable Jira intake (not yet implemented; this is how it will work):

1. **Poll**: the daemon asks Jira every `interval_seconds` for issues matching a JQL query
2. **Intake**: each new issue becomes a task in the chosen repo, with a goal template that
   includes the issue's details, a link back to Jira, and a closing instruction
3. **Link**: the task's thread saves the Jira URL and other metadata
4. **Transitions**: agent tool calls update the Jira issue as the task moves through states

The goal template for an issue has a fixed shape (example for bug KMP-123):

```
KMP-123 · Crash on app start
Jira: https://acme.atlassian.net/browse/KMP-123 · Bug · Priority: High · Status: To Do
Labels: ios, urgent; Components: mobile-app; Reporter: alice

Description (from Jira):
Steps to reproduce:
1. Open the app
2. Click Settings
3. App crashes

Engineer: read the description above and the Jira issue, implement a fix, open an MR to
main, and move the issue to In Progress when you open the MR. Transition it to Done when
the MR is merged.
```

For a read-only repo:

```
Engineer: the repo is in read-only mode. Run the check, read the description, and propose
changes in a comment on this task. Do not open an MR or transition the issue.
```

## How issues arrive

Two ways:

### Polling (default)

The daemon loop `daemon:jira-poll` runs every `interval_seconds` seconds (configured
in `jira.intake.interval_seconds`, min 60). It searches Jira for issues matching
the JQL, checks which ones are new, and creates tasks.

```yaml
jira:
  intake:
    enabled: true
    jql: "project = KMP AND type = Bug AND status = \"To Do\""
    repo: my-app
    interval_seconds: 300
    max_per_pass: 50
```

Each issue becomes one task. The daemon:

1. Searches Jira (`search` action, `daemon:jira-poll` actor)
2. For each new issue, calls `create_task` (internal, no journal entry)
3. Records the link between the task and the issue (no journal entry)

A failed search logs an error but does not discard old baselines — next tick re-reads
and reports what changed, so a merge you could not see is announced late rather than never.

### Webhooks (future)

A webhook receiver at `/webhooks/jira` listens for issue state changes. Jira must be
configured with a public address (not localhost) so it can dial back.

For now, polling is the only way.

## Agent tools (for the task's worker)

These tools are available in an agent's context when it is working on a task. Every
call is journaled under `agent:<role>@<task_id>`.

### `jira_issue(key: str)` → dict

Fetch an issue by key. Returns the full shape with all fields.

### `jira_transition(key: str, to_status: str)` → dict

Move an issue to a status. `to_status` is matched case-insensitively against the
target status name (e.g., "In Progress") or the transition name (e.g., "Start").

### `jira_comment(key: str, text: str)` → dict

Post a comment on an issue.

All three tools are journaled under the action that uses them, with the agent's role
as the actor.

## How Jira will be joined by others

Jira is the first provider of tasks from outside. GitLab is next.

Sisyphus will have:
- `core/gitlab.py`: a client like `core/jira.py`
- GitLab webhooks or polling in the daemon loop (`daemon:gitlab-poll`)
- The same agent tools: `gitlab_issue`, `gitlab_comment`, etc.
- One journal table, one Integrations page

Both will write to the same `integration_events` table. The page groups by provider
(`provider = 'jira'` or `provider = 'gitlab'`). A GitLab issue becomes a task the
same way a Jira issue does — the executor doesn't care which tracker it came from.

---

See also: [`PRODUCTION.md`](PRODUCTION.md) for readiness checks, [`GOAL.md`](GOAL.md)
for the north star.
