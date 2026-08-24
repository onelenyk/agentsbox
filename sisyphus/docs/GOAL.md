# The goal: Sisyphus builds Sisyphus

> Living document. The numbers in it are not typed by hand — they come from
> `python3 tools/goal_report.py`, which recounts them from git, `sisyphus.db`
> and [`known-gaps.md`](known-gaps.md) every time it runs.

## The north star

**Most of what lands on this repo's trunk arrives through a Sisyphus merge
request, delivered without a human turn in the thread.**

[`../../docs/IDEA.md`](../../docs/IDEA.md) states the ultimate goal as "you
describe a task, the system produces a reviewed, validated change — without you
in the loop". The trunk says otherwise. Over the last 30 days, 198 commits
landed on `main` and 38 of them came in through `Merge sisyphus/task-*`. The
other 160 were written by a human and an agent in a terminal — the exact
workflow Sisyphus exists to replace.

That gap is the goal. It is one number, it is already recorded in history no
one can retouch, and everything else the system needs — reliable runs, review
that stands on evidence, a loop that does not stop to ask — is a precondition
for moving it rather than a separate ambition.

## The scoreboard

```
python3 tools/goal_report.py            # last 30 days
python3 tools/goal_report.py --days 7 --json
```

| Metric | What it counts | Baseline 2026-08-23 | Target |
|---|---|---|---|
| **Self-build** | commits on the trunk delivered through a `Merge sisyphus/task-*`, over all non-merge commits | **19.2%** (38 / 198, 24 merges) | 50% |
| Autonomy | delivering tasks (those that opened an MR) whose thread has no human message | 65.6% (21 / 32, 11 questions asked) | 95% |
| Evidence | merged MRs carrying *both* a reviewer's approval and a check the daemon ran itself | 15.4% (4 / 26; 11 approved) | 100% |
| Reliability | terminal tasks that did not fail | 92.2% (7 of 90 failed, 6 of them silently) | 99% |
| Known gaps | entries in `known-gaps.md` not marked CLOSED | 1 open, 12 closed | 0 open |

Definitions worth stating, because a metric is only as honest as its
denominator:

- **Commits, not merges.** One merge may carry seven commits. What matters is
  how much of the code an agent wrote, not how many times we pressed merge.
- **Delivering tasks only.** Most rows on the board are autopilot scans and
  review-of-MR passes; counting those in *autonomy* would flatter the number
  without anything having shipped.
- **Evidence is conjunctive.** An approval with no recorded check is an agent's
  word, and a green check nobody signed off on is a number nobody read. The
  metric counts a merge only when both are on the record.
- **Silence counts as open.** A known-gap entry with no status line is counted
  open — the safe way to be wrong about our own ledger.

## Milestones

| | Self-build | Also true | Meaning |
|---|---|---|---|
| **L1 — Supply** | 30% | reliability ≥ 97% | The system has a steady diet of its own backlog, and a run that starts finishes. |
| **L2 — Majority** | 50% | autonomy ≥ 85%, evidence 100% | More than half the trunk is agent-built, and every merge cites a record. |
| **L3 — Default** | 75% | autonomy ≥ 95%, 0 open gaps | Working in a terminal is the exception that has to be justified. |

We are below L1.

## The rule that actually moves the number

Nothing in the code raises *self-build* on its own. The number moves when work
is **routed** to Sisyphus instead of to a terminal session. So: a task that fits
the system — scoped, in this repo, with a check that can prove it — goes to the
system by default. Doing it by hand is the exception, and the exception gets
named out loud when taken. Every hand-written commit on the trunk is a vote
against the goal; sometimes it is the right vote, but it should be a decision
rather than a reflex.

## The backlog

Each item is grounded in something observed in the run record, and names the
metric it moves. They are ordered by leverage, not by size.

### 1. Feed the loop from the ledger — *self-build*
The system's throughput is capped by its supply of work. `known-gaps.md`,
`docs/`, and the failure record are a backlog it could be given on a schedule
through the autopilot machinery that already exists. This is configuration and
routing, not new surface.

### 2. A worker that returns nothing must not end the task — *reliability*
Six of the seven failed tasks died the same way: `mr-reviewer ended without a
result` / `agent_no_result`. The decider already retries an empty or malformed
run (d938e80); worker runs get no such guard, so one silent subprocess kills a
task that had done nothing wrong.

### 3. Stop asking for permission the record already grants — *autonomy*
Of the 11 questions asked on delivering tasks, several read: *the reviewer
approved, the check passed 1030, the MR is open — shall I?* The orchestrator
already has all three facts on the MR row. Asking there converts a finished
task into a blocked one and puts the human back in the loop at the last metre.

### 4. Every merge cites its check — *evidence*
Twelve of the 26 merged MRs carry `review_status = NULL`: they were merged
through a path that never recorded a verdict. The gate and the recording exist
(f205769, fd2f86c); what is missing is the paths that bypass them.

### 5. Close the last open gap — *known gaps*
*A worker's tool roster carries the user's whole desktop.* The plugin and skill
share is stripped (8638138); globally configured MCP servers still merge into
every worker run. Both surface and spend.

---

*How to update this file: run the report, replace the baseline row, and — if a
milestone was reached — say so. Do not edit a number without re-running the
command that produced it.*
