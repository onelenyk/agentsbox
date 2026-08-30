# Test coverage — what the suite actually reaches

Measured 2026-08-30 against `main` at 1161f80. Every number below was produced
by running the suite, not by reading it. Where a number misleads, the section
says so — a coverage report that flatters the suite is worse than none.

## How to reproduce

`coverage` is not in `requirements-dev.txt` (see "Follow-ups" — adding it is a
change outside `tests/`), so it was installed into a throwaway venv:

```bash
python3 -m venv --system-site-packages /tmp/cov-venv
/tmp/cov-venv/bin/pip install coverage
cd sisyphus
/tmp/cov-venv/bin/python -m coverage run --source=core,web,tools \
    -m pytest -q --ignore=tests/e2e -p no:cacheprovider
/tmp/cov-venv/bin/python -m coverage report -m --sort=miss
```

## Headline

| | |
|---|---|
| Tests | **1150 passed, 0 failed**, 55–57s wall |
| Line coverage, `core` + `web` + `tools` | **82%** (7254 statements, 1285 missed) |
| Line coverage, excluding `tools/` scripts | **85%** |
| Line coverage, safety-critical six (below) | **90%** |
| Tiers | 530 `unit` · 570 `component` · 43 `process` · 7 unmarked |

The tier split is the healthier number. Half the suite drives the real thing —
real routes, real git repositories, a real MCP server — rather than mocks. For a
system whose job is spawning subprocesses, that ratio is the thing worth
protecting; the percentage is secondary.

## By module, grouped by blast radius

Coverage matters in proportion to what a miss costs. Ranked that way rather than
alphabetically:

### Tier 1 — the guarantees the README makes

| Module | Cover | Missed |
|---|---|---|
| `web/auth.py` | **100%** | — |
| `core/verify.py` | 93% | 5 |
| `core/mergerequests.py` | 94% | 20 |
| `core/worker.py` | 92% | 17 |
| `core/sandbox.py` | 89% | 7 |
| `core/orchestrator.py` | 89% | 46 |
| `core/launcher.py` | 85% | 29 |

Strong. The misses here are overwhelmingly defensive `except Exception` arms and
platform branches, not untested logic — with two exceptions named in "What the
numbers do not say".

### Tier 2 — where real failures actually come from

| Module | Cover | Missed |
|---|---|---|
| `core/autopilot.py` | 85% | 32 |
| `core/orphans.py` | 78% | 49 |
| `core/planner.py` | 77% | 99 |
| `core/daemon_ctl.py` | 59% | 62 |

This is the report's main quantitative finding. Of the 8 failed tasks in the
daemon's own database over the last 30 days, **5 were autopilot cron runs**
(`Nightly repo scan`, `morning-mr-review`). The subsystem that produces most
real-world failures is also the least-covered part of `core` — and it is the
one that runs unattended at night, where a silent failure goes unseen longest.

`daemon_ctl.py` at 59% is process lifecycle: start, stop, health, install. Low
coverage there is defensible (it drives launchd and real PIDs) but it is also
where a bad day starts.

### Tier 3 — `tools/`, and a genuine hole

| Module | Cover | Note |
|---|---|---|
| `tools/agent_mcp_stdio.py` | **0%** | the bridge every worker talks through |
| `tools/fetch_jira.py` | 0% | |
| `tools/smoke_mcp_stdio.py` | 0% | |
| `tools/describe_image.py` | 0% | |
| `tools/run_grep.py` | 0% | |
| `tools/goal_report.py` | 69% | |

These 0%s were initially assumed to be a measurement artifact — coverage does not
follow subprocesses. They are not. Searching the suite for any execution of
these scripts returns exactly one line:

```
tests/test_worker.py:121:  assert mcp["command"][-1].endswith("agent_mcp_stdio.py")
```

That asserts the *path is wired*, not that the bridge runs. `tests/test_mcp_server.py`
mocks `subprocess.run`. **No test executes `tools/agent_mcp_stdio.py` at all.**

That matters more than its 36 statements suggest. The worst incident this project
has documented — `docs/known-gaps.md`, closed 2026-08-23 — was an engineer
retrying `open_mr` 160 times against an empty tool roster until it burned the
1800s timeout. The roster is served through this bridge. The component whose
failure caused the most expensive documented outage has zero test execution.

## What the numbers do not say

Three gaps found by reading the missed lines rather than counting them. None
would be visible from the percentage — two live in modules covered at 90%+.

### G1 — the merge gate ignores `check_head` (high)

`mergerequests.set_check_result` records `check_head`, the commit the check
actually ran against, and its docstring explains why: "so a later reader can tell
a current verdict from one the branch has since moved past".

`web/agent_mcp.py:366` honours that. It computes `covers_head` and tells a
reviewer agent, in careful prose, that a verdict not covering the current commit
"is history".

`mergerequests.merge_block_reason` — the function that actually **enforces** the
gate — never reads `check_head`:

```python
if (mr.get("check_status") or "").strip() == "failed":
    return "the daemon's own check FAILED on this branch"
return None
```

So: a check passes against commit A, a worker pushes commit B, and the merge
button opens. The agent-facing advisory path is strictly stricter than the
enforcement path. The reviewer is told the verdict is stale; the gate is not.

Every caller of the enforcement path inherits this — `web/routes.py:1410` and
`:1457` (the merge and land buttons), `core/planner.py:664` (auto-merge),
`core/copilot.py:49`.

Confirmed by running it, not by reading it:

```python
>>> stale = {"id": "mr-x", "review_status": "approved", "check_status": "passed",
...          "check_head": "aaaaaaaa", "check_command": "pytest -q"}
>>> mergerequests.merge_block_reason(stale)
None
>>> mergerequests.gate_state(stale)
'ready'
>>> mergerequests.verdict_line(stale)
'review approved · check passed (pytest -q) against aaaaaaaa'
```

The last line is the sharp end: the record honestly names the commit the verdict
covers, and the gate calls it ready anyway without ever asking whether the branch
is still there. The bookkeeping is right; only the decision is not.

**Test to write:** an MR with `check_status='passed'`, `check_head=<A>`, branch
head at `<B>` — assert `merge_block_reason` is not None. It currently is None.
This is a failing test, i.e. a bug, not a coverage gap.

**Cost is slightly more than it looks:** `merge_block_reason(mr)` takes only the
row, so it cannot compare against anything. The fix either resolves the current
head inside it (via `diff_range`, which does I/O — changing the function's
character) or takes the head as an argument and updates the four call sites. That
is a design call, not a patch.

### G2 — the no-sandbox path is skipped, not asserted (medium-high)

`core/sandbox.py:54` returns `None` when the host has no backend (any non-Darwin
platform, or a macOS without `sandbox-exec`). `wrap()` then falls through to
`return args` — the worker runs **unsandboxed**.

The suite does not assert anything about that path:

```
tests/test_sandbox.py:33:  pytest.skip("no sandbox backend on this platform")
```

It skips. And CI runs `macos-latest` only, so the `None` branch is never
exercised anywhere — not locally, not in CI.

> **Correction.** An earlier draft of this section said the run proceeds
> *silently*. That was wrong, and reading the call site rather than the module
> is what showed it: `core/launcher.py:322` already logged
> `"no filesystem sandbox on this platform (%s) — worker runs unconfined"`.
>
> What the mistake uncovered is a smaller but real hole in the neighbouring
> branch. The warning sat behind `elif not sandbox.available()`, and a sandbox
> switched off with `SISYPHUS_SANDBOX=0` is still an *available* one — so
> `enabled()` was False, `available()` was True, and the one unconfined case an
> operator actually chose produced no log line at all. The genuinely silent path
> was the deliberate one, not the accidental one.

**Test to write:** monkeypatch `platform.system()` to `"Linux"`, assert the
intended behaviour — and cover `SISYPHUS_SANDBOX=0` separately, since the two
unconfined states reach the launcher down different branches. The intent needs
deciding first: refuse to launch, or warn and proceed? Refusing outright would
make the daemon unusable on Linux, where no backend exists yet.

### G3 — the README overstates the gate (low, doc-only)

README: "A merge is allowed only when that approval exists **and** the repo's own
`check:` command passed."

`merge_block_reason`'s own docstring: "A missing check is NOT blocking — a repo
may configure none, and 'not_run' is recorded honestly — but a FAILING one is."

The code is right and deliberate; the README is wrong. Worth fixing in the README
rather than the code. `tests/test_verify.py:136` pins `not_run` at the *recording*
level; nothing pins the intended non-blocking semantics at the *gate* level, so a
future change could silently invert it.

## `web/routes.py` — 67% is not what it looks like

1111 statements, 372 missed, the largest single block of uncovered lines in the
repo. Mapping every missed line onto its route handler:

```
routes with >=90% of body unexecuted:  0 of 99
```

Not one of the 99 routes is untested. The 67% is error branches and htmx fragment
variants, not whole endpoints. Worst-covered handlers:

| Route | Missed / body |
|---|---|
| `POST /api/board/chat` | 18/22 |
| `POST /api/runners/{id}/smoke` | 14/24 |
| `POST /api/mr/{id}/land` | 14/18 |
| `POST /api/tasks/ai-parse` | 12/18 |
| `POST /api/board/act` | 11/14 |

The LLM-backed routes (`board/chat`, `ai-parse`, `board/act`) are thin for an
obvious reason — they call a model. `POST /api/mr/{id}/land` at 14/18 is the one
worth raising: it is a merge-adjacent action, and it is the least-covered route
in Tier 1 territory.

## Ranked gaps

| # | Gap | Severity | Status |
|---|---|---|---|
| 1 | G1 — gate accepts a stale check | high | **fixed** 2026-08-30 — `merge_block_reason(mr, head=None)` resolves the head itself; 7 tests |
| 2 | `tools/agent_mcp_stdio.py` at 0%, never executed | high | **fixed** 2026-08-30 — 18 `process`-tier tests spawn the real bridge; a third bug fell out of it |
| 3 | G2 — no-sandbox degradation untested and unexercised in CI | medium-high | **fixed** 2026-08-30 — `unconfined_reason()` + opt-in `sandbox.require`; 11 tests |
| 4 | `autopilot` / `planner` / `orphans` under-covered where failures concentrate | medium | **worked** 2026-08-30 — see "Update" below; 85/77/78% → 91/88/90%, and two live bugs fell out of it |
| 5 | `POST /api/mr/{id}/land` thin | medium | open — one component test |
| 6 | G3 — README contradicts the gate | low | **fixed** 2026-08-30 |
| 7 | AUTH — token guard covered `/api/v1` only, UI ungated | high | **fixed** 2026-08-30 — guard covers the whole surface; 6 tests |
| 8 | METRIC — `evidence` window straddles the check landing | medium | **fixed** 2026-08-30 — counted from `CHECK_GATE_SINCE`; 21.4% → 75.0% |

Items 7 and 8 were found during the same pass but are not coverage gaps, so they
sit outside the numbered list above. All five fixes landed together; the suite
went 1150 → 1174 tests, still green.

## Update — 2026-08-30, gap 4

The three modules where real failures concentrate were taken next. 66 tests
added; `core+web+tools` 82% → 84%.

| Module | Before | After |
|---|---|---|
| `core/autopilot.py` | 85% | **91%** |
| `core/planner.py` | 77% | **88%** |
| `core/orphans.py` | 78% | **90%** |
| `core/ai_assist.py` | 89% | 92% |
| `core/copilot.py` | 85% | 92% |

The last two moved without being aimed at: unifying the JSON parser moved 16
statements into `ai_assist` and deleted 12 from `copilot`, so read those two
rows as "the shared parser is tested", not as new tests for either module.

The percentages are the least interesting part again. Writing the tests turned
up two live bugs, both documented in `docs/known-gaps.md`:

- **An automation that could never fire looked exactly like one waiting.** A
  cron apscheduler cannot parse stores `next_run` NULL, the card renders NULL
  as the same `—` a webhook and a paused autopilot show, and `tick_once` only
  looks at rows whose `next_run` is set — so nothing warned again after the
  create-time log line. `autopilot.schedule_problem` now names it.
- **Three copies of one JSON parser, drifted apart, both broken.** planner and
  copilot spelled the fallback `\{.*\}` (greedy — one aside in braces swallows
  the whole reply), ai_assist spelled it `\{.*?\}` (lazy — truncates every
  nested object, and a brief's steps are objects). Both returned None, which
  every caller reports to the user as "the model didn't give me anything
  usable". One `ai_assist.find_json` now counts braces.

What the tests actually pin, beyond the count: the reaper's `unverified` and
`failed` buckets — the module's stated promise that it never counts an unkilled
process as killed — had no test on their failure paths at all, nor did its
dry run. `_argv_linux` had never executed anywhere, CI being macOS-only, and
now runs against a fake `/proc`. `start_plan_turn`'s double-submit guard and
its background-thread error capture were untested, and a wedged plan (flag set,
no reply, no error, every later Send ignored) was one exception away.

Still open from the list above: `tools/agent_mcp_stdio.py` at 0% (gap 2) and
`POST /api/mr/{id}/land` (gap 5). `planner.project_context` and the
`daemon_ctl` process-lifecycle paths remain the largest deliberate misses.

## Update — 2026-08-30, gap 2, and a correction to this report

`tests/test_agent_mcp_stdio.py` spawns the real script as a subprocess, speaks
JSON-RPC to its stdin, and reads its stdout, with the daemon's own `/mcp/agent`
router answering on a real socket (that router alone — the full app's lifespan
starts the orchestrator and autopilot loops, and this is a test about a socket).
18 tests: the roster arrives and is role-scoped, a tool call carries its
arguments and writes the thread, a notification draws no reply and does not
desync the stream, a malformed line is a parse error the process survives, and
an unreachable daemon names the URL rather than hanging. Checked by mutation,
not by passing: removing the Authorization header fails 7 of them, and
answering notifications fails exactly the one about notifications.

**Correction.** This report said the 0%s in `tools/` "were initially assumed to
be a measurement artifact — coverage does not follow subprocesses. They are
not." That was true when written and is now the reverse: `agent_mcp_stdio.py`
still reports 0%, and that 0% is now exactly the artifact the earlier draft
suspected. Read it as unmeasured, not untested. Making it a real number needs
`COVERAGE_PROCESS_START` plumbed into the spawned environment — worth doing
when the CI coverage job below lands, not before.

The third bug of the week fell out of it, and it is the one that matters most:
`/mcp/agent` answered `tools/list` with `{"result": {"tools": []}}` and HTTP 200
for a session whose agent is not in the table. Deleting a custom agent does not
revoke the sessions running as it, so that is reachable by an operator tidying
up the Agents page while a worker runs. An empty roster is the exact shape of
the 160-retry incident, and a worker holding no tools cannot even call `finish`
— the run can only end at its timeout. It is a `-32002` error now.

## Recommendation on a floor

Do **not** set a single global percentage. It would push work toward `tools/`
and `daemon_ctl.py`, which is the opposite of where risk lives, and it would read
as progress.

Better: a per-directory floor, failing CI on regression rather than mandating a
climb —

```
core/     ≥ 88%   (today: ~89%)
web/      ≥ 78%   (today: ~81%, dominated by routes.py)
```

— plus a named exemption list for `tools/` scripts, so their 0% is a recorded
decision rather than an oversight.

## Follow-ups outside `tests/`

Blocked on the current milestone's scope (`tests/**`), listed so they are not
lost:

- add `coverage` (or `pytest-cov`) to `requirements-dev.txt`
- add the coverage run + per-directory floor to `.github/workflows/ci.yml`
- CI runs `macos-latest` only; a Linux job would exercise the `sandbox.backend()
  is None` branch that G2 is about
- G1 is a code fix in `core/mergerequests.py`
- G3 is a README fix
