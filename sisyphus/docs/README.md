# Docs

Living documents — kept current, read these first:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the domain model and engine: Task + Thread,
  the cold orchestrator loop, workers, merge requests.
- [`known-gaps.md`](known-gaps.md) — honest ledger of what is still broken or
  missing, each entry moved to CLOSED only when the fix ships.
- [`adr/`](adr/) — architecture decision records (why the design is the way it is).

Archive — completed plans and executed test campaigns, kept as the record of how
features came to be (each file carries an *Archived* stamp with its outcome):

- [`archive/bucket-b-roadmap.md`](archive/bucket-b-roadmap.md)
- [`archive/agents-and-orchestrator-as-agent.md`](archive/agents-and-orchestrator-as-agent.md)
- [`archive/multi-repo-fanout.md`](archive/multi-repo-fanout.md)
- [`archive/local-merge-requests.md`](archive/local-merge-requests.md)
- [`archive/real-world-tests.md`](archive/real-world-tests.md)

A plan doc lives in `docs/` while it is being executed and moves to `archive/`
when it is done — it never gets silently deleted.
