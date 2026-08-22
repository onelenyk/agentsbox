# Testing — taxonomy and strategy

Every test module belongs to exactly one tier, declared in `tests/tiers.py`
(one reviewable map; an unclassified file fails collection with instructions).
Markers are stamped automatically — run a tier with `pytest -m <tier>`.

## The tiers

| tier | what it proves | speed | count (2026-08) |
|---|---|---|---|
| `unit` | the logic is right — in-process, tmp SQLite at most | ~9s | 474 |
| `component` | one subsystem drives the real thing it owns: routes via TestClient, git actions on real temp repos, the MCP server | ~30s | 531 |
| `process` | the OS story holds: spawning, signals, sandbox, timeouts | ~7s | 37 |
| `e2e` | a whole path leaves real artifacts: task → commit → MR → check → merge (machine e2e); browser behavior (Playwright) | ~5s* | 8 |

\* the browser e2e auto-skips without Playwright installed.

Definitions, sharpened by what actually broke here:

- **unit** may touch a tmp database (store IS logic in this codebase) but never
  a subprocess and never git. If it shells out, it is not a unit test.
- **component** exists because our worst bugs lived *between* correct units:
  a route that never queued the task it built, a value computed and never
  rendered (`test_page_smoke` asserts the number reaches the page). Real git
  repos in tmp dirs, real SQL, no mocks of our own code.
- **process** is the honest-skip tier: some of it cannot run everywhere
  (Seatbelt blocks `ps`, so kill-paths skip in a sandboxed worktree — the skip
  *probes the capability* instead of guessing from env vars).
- **e2e** deliberately excludes the model: what an agent decides cannot be
  asserted and costs money; what the machine does with decisions is what
  breaks silently. Model-in-the-loop checks live outside pytest (below).

## Order of development — how stability is actually built

The order matters because each layer only means something on top of a stable
layer below it:

1. **unit, with the module.** Written together with the code. Fast enough to
   run on every edit (`pytest -m unit`, ~9s).
2. **component, per surface.** Every route, every git action, every page gets
   one the moment it exists. This is the layer that catches "correct but never
   arrives".
3. **process, per OS interaction.** Only where we genuinely manage processes.
   Small on purpose: these are the slowest to debug and the most
   environment-sensitive.
4. **e2e, one per critical path.** Not per feature — per *path a user bets
   on*. Today: the change-shipping path. Few and stable, or they rot.
5. **Above pytest** (planned, run by nightly autopilots, not the MR gate):
   - **canary** — a real task through the full cycle with the model in the
     loop; the only check that covers what e2e deliberately excludes
   - **persona evals** — golden tasks asserting behavioral contracts (does the
     orchestrator ask on a vague goal; does the engineer restate; does the
     explorer report coverage)
   - **browser suite** — the Playwright tier, run where Playwright exists
   - **mutation pass** — automated version of the mutation-check discipline

## Rules that keep the pyramid honest

- **A regression test lands at the LOWEST tier that reproduces the bug.**
  Every closed entry in `known-gaps.md` has one.
- **Every guard gets mutation-checked once**: break the implementation, watch
  the new test fail, restore. A test that survives its mutation is asserting
  nothing. (Beware same-size same-second file swaps — Python's bytecode cache
  can serve the stale module; nudge mtime.)
- **Environment limits are probed, not assumed** — a skip must state the real
  capability that is missing, and errors/writes always run.
- **The MR gate runs everything** (`check:` in config.yaml, ~50s). Tiers exist
  for the developer loop and for the nightly split, not to let the gate skip.

## Running

```bash
pytest -m unit                 # every edit, ~9s
pytest -m "unit or component"  # before commit, ~40s
pytest -q                      # what the MR check gate runs, ~50s
pytest -m e2e                  # the shipping path (browser part needs Playwright)
```
