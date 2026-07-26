# ADR 0001 — Board-driven orchestration for autonomous SDLC

**Status:** Superseded by [ADR 0002](0002-task-thread-orchestration.md) — the
extended card lifecycle, decision journal, and dual FSM described below were
judged too complex in practice and replaced with a 2-entity, 0-state-machine
model before this design was built. Kept for history; do not implement against
this document.
**Date:** 2026-07-23

## Goal

Sisyphus is evolving into an **autonomous SDLC system**: you create a card with a
description, and the system takes it from intent to delivered work (analysis →
Fix Plan → code → merge request → merged), pausing for a human only where a
human decision is genuinely needed. The roadmap climbs an autonomy ladder:
make MRs → handle the MR lifecycle → autonomously deliver whole features.

## The problem with the current shape

Today the flow is driven by a **static YAML pipeline** (a fixed DAG of agent
steps), with the DB recording state. This is predictable but can't express "it
depends", which is most of real SDLC. It also has no notion of a long-lived task
that reacts to things happening later and outside Sisyphus (a review comment, a
CI failure, a human answer).

## The decision

**Sisyphus becomes board-driven, orchestrated by an agent, with all
communication through role-scoped MCP tools.**

The board (the DB) is the single source of truth. Process is driven by an
**orchestrator agent**, not by hardcoded pipeline logic. Every actor — human,
orchestrator, worker agents — changes the board **only through validated,
role-scoped MCP tools**. Files remain, but as deliberate attachments, not as a
hidden communication channel.

### The seven pillars

1. **The board is the single source of truth.** No load-bearing state lives in
   any agent's context or in RAM. Everything durable and inspectable is on the
   card.

2. **The orchestrator is a function, not a live process.** It is modeled as
   `(card's decision journal + an event) → next decision`. It is
   **cold-per-event**: there is no warm, always-on orchestrator session.
   Continuity of thought comes from a persistent **decision journal** on the
   card (short "decided X because Y" entries) — not from a warm LLM session.
   Therefore a daemon restart is a no-op: the orchestrator is rebuilt from the
   journal and continues seamlessly.

   *Why cold, not warm:* SDLC events are sparse (minutes to days apart), so a
   warm session buys almost no latency while costing memory, context-window
   management, and crash-fragility. The board is already a compact memory, so
   rehydration is cheap. (A short-TTL warm cache for rare bursts of back-to-back
   events is a possible later optimization, not a foundational choice.)

3. **Event-driven, not polled ticks.** An event — a worker finished, a human
   replied, later an MR/CI webhook — triggers exactly one orchestrator reasoning
   pass, which emits an action and a journal entry, then the process ends.

4. **Communication is MCP tools, not files.** The daemon serves each role
   **exactly its allowed tool set** (filtered per connection by a scoped session
   token → role). An Explorer sees `comment / attach / finish`; an Engineer also
   sees `commit / open_mr`; a Reviewer sees `approve / request_changes`. The
   agent physically cannot call outside its profile.

5. **"When to use a tool" is enforced by the tool, not the prompt.** An LLM does
   not follow prose 100%; a tool that rejects invalid calls does. "When" is
   encoded as **preconditions** (e.g. `open_mr` fails without a prior `commit`; a
   reviewer can only finish via `approve`/`request_changes`; `finish` is the only
   way to end a step). The prompt describes intent; the tool guarantees it.

6. **Files stay, as intentional artifacts.** An agent calls
   `attach(file, description)` to declare a deliverable — no more auto-scanning
   the output directory. Large artifacts still live on disk; the tool call is the
   pointer plus meaning.

7. **The orchestrator coordinates; it does not judge quality.** Its action space
   is small and bounded (spawn-agent, ask-human, move-card, post-note,
   mark-done). Evaluation ("is this good / done?") is **delegated** to a
   reviewer/QA agent it spawns — it never judges an agent's work itself.

### Human-in-the-loop

A card can enter a `waiting_human` state. The orchestrator parks it and posts the
question to **Activity**; the human replies in Activity; the orchestrator picks
that up on its next event and continues. The Activity/comments channel already
exists and is the human↔orchestrator interface.

## What this reuses vs. changes

This is a **re-arrangement of the driver, not a rewrite.** The following already
exist and remain the foundation:

- **The daemon** (`web/app.py`) — the single always-on host; owns the one
  scheduler and executes work in its event loop.
- **The control surface** (`web/api.py` JSON API + thin `mcp_server.py` client +
  the htmx board) — the orchestrator's and human's action space.
- **Activity / comments** — the human channel.
- **git worktree isolation** and **fail-fast `finish`**.

What changes:

| Today | Target |
|---|---|
| Scheduler runs a fixed YAML pipeline | Daemon runs the orchestrator loop over the board |
| Pipeline YAML is the driver | Pipeline YAML is an optional *template* the orchestrator may use |
| File contract (events.jsonl / result.json / output-dir scan) | Agent-facing MCP, scoped by a session token |
| `register_output_files` scans the dir | `attach(file, description)` |
| Card states: backlog → running → done | + `investigating / waiting_human / implementing / mr_open / …` |
| `agents/*.yaml` tool list is declared but unenforced | Tool list enforced at the MCP protocol |

## Consequences

- **Gain:** flexibility (handles "it depends"), durability (restart-safe,
  fully observable board), a uniform validated action surface for every actor,
  and a clear path up the autonomy ladder without rewriting execution.
- **Cost:** non-determinism (an agent drives the process), token cost per
  orchestration decision, and the risk of thrash — mitigated by (a) the
  board-externalized decision journal (every decision is visible and replayable)
  and (b) a deliberately small, bounded orchestrator action set.
- **Key feasibility item to verify before building the MCP contract:** that
  `opencode` can be pointed, per run, at a remote MCP server with a passed
  session token and a per-connection tool allowlist.

## Rough sequencing (dependency order)

1. **Decision journal + extended card lifecycle states** — the durable substrate
   everything else stands on.
2. **Agent-facing MCP surface** (scoped by session token) — replace the file
   contract with role-scoped tools whose "when" is enforced as preconditions.
3. **Orchestrator loop** — the `(journal + event) → decision` function with a
   small bounded action set, running side-by-side with the existing pipeline
   engine (gated by a flag) until it supersedes it.
4. Then: reactive external events (MR / CI), and eventually autonomous features.

Each phase is incremental and must keep the test suite green (strangler-style,
not a big-bang rewrite).
