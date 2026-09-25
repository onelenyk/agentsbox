# ADR 0002 — Task + Thread orchestration

**Status:** Accepted, implemented (Phases 1–3), verified live end-to-end.
**Date:** 2026-07-25
**Supersedes:** [ADR 0001](0001-board-driven-orchestration.md)

## Why this replaces ADR 0001

ADR 0001 proposed a card journal, an extended lifecycle (`investigating` /
`waiting_human` / `implementing` / `mr_open` / …), and a dual FSM (card status
+ phase). Before any of it was built, the direction was rejected as too
complex: the system was already made of many separate moving parts, and the
goal was to make it simpler to reason about, not richer. The replacement
keeps ADR 0001's core insight (orchestration by an agent reading durable
state, not a hardcoded pipeline) but collapses the data model to almost
nothing.

## The decision

**Two entities. Zero state machines.**

- **Task** — a goal, an optional repo, and a status: `active`, `waiting`,
  `done`, or `failed`. That's the entire lifecycle; there is no separate
  "phase" dimension.
- **Thread** — an append-only list of messages on a task (`author`, `kind`,
  `text`, optional `attachment`, an atomically-allocated `seq`). One universal
  verb — *post a message* — is how every actor (orchestrator, worker, human)
  communicates. There is no separate "journal" from "activity" from
  "comments"; it's one thread, rendered in full as context for every decision.

**The orchestrator is a cold, stateless function**: `(thread + event) → one
decision`. Not a warm/live process — there is no long-running orchestrator
session to manage or crash. Continuity comes entirely from re-reading the
thread; a daemon restart is a no-op (`reconcile_on_boot()` just synthesizes a
"worker finished" event for any task left mid-flight and re-emits it). Its
action space is small and bounded: `spawn_worker`, `ask_human`, `note`,
`done`, `fail`, `noop`. It never judges whether a worker's answer is *good* —
that's delegated to a spawned reviewer role, same as ADR 0001 intended.

**Roles are light presets**, not a bureaucracy: a persona string plus an
allowed-tool subset. `explorer` and `engineer` can `finish`; `reviewer`
`approve`s or `request_changes`s instead. That's the entire role system.

**Agent communication is MCP tools, not files** — this part of ADR 0001 held.
A worker gets a session token scoped to its role; the daemon serves it only
its allowed tools (`get_context`, `comment`, `attach`, `commit`, `open_mr`,
`finish` / `approve` / `request_changes`), and a tool's own preconditions
enforce "when" (`open_mr` requires a prior `commit`) rather than relying on
the prompt alone.

## Hard-won implementation lessons (read this before touching worker spawning)

Getting a real opencode worker to actually use these tools took a full day of
live debugging, because failures here are *silent* — the worker exits "clean"
with no error, and the only symptom is an empty thread. Three independent bugs
stacked on top of each other:

1. **Transport: local (stdio), not remote (HTTP).** opencode's remote-MCP
   transport was tried first and proved unreliable in practice (worked once,
   then failed identically on a re-run with no code change). Every other MCP
   server on the host machine, and Sisyphus's own control-plane
   (`mcp_server.py`), already use local/stdio — `tools/agent_mcp_stdio.py`
   mirrors that exact pattern: a thin stdin/stdout JSON-RPC bridge that
   forwards each request to the daemon's `/mcp/agent` over plain HTTP.

2. **The old file-based contract must be fully disabled, not just unused.**
   `core/launcher.py` originally injected `SISYPHUS_AGENT_TOOL` (the old
   file-CLI path) unconditionally and gated success on a `result.json`
   existing — leftover from the pre-MCP system. Worse, when that legacy gate
   failed, its own error text ("...must call `$SISYPHUS_AGENT_TOOL` finish")
   got posted to the thread, which is replayed into every subsequent retry's
   prompt — actively teaching each retry the wrong reporting mechanism.
   Fixed with `launch_agent(..., skip_legacy_contract=True)`, which drops
   both the env var and the result.json gate for MCP-based workers; the
   caller (`core.worker`) determines success purely from whether the MCP
   session was closed via a real tool call.

3. **`--pure` is the wrong tool for isolating the user's environment.**
   `--pure` was meant to stop a user's global opencode plugins from
   hijacking the run. It does that, but was confirmed live (via
   `opencode debug config` and a direct tool-listing run) to **also strip
   project-local MCP servers** — including our own. The fix is a
   project-level `opencode.json` override of `"plugin": []`, which disables
   just the plugin and leaves the project's own `"mcp"` entries intact.

4. **A global custom agent persona can survive `"plugin": []`.** Even with
   the plugin disabled, a custom *agent* registered in the user's global
   config (a separate config key from plugins) can still be selected as the
   default primary agent — and its system prompt can hardcode routing every
   tool call through a "skill"/`skill_mcp` indirection layer that never sees
   raw project-local tools. The fix is `launch_agent(..., agent="build")`,
   forcing opencode's own neutral built-in agent instead of whatever custom
   persona the user's environment happens to register.

None of this is specific to Sisyphus's own repo being a bad test target,
except one confusion mode worth knowing about: testing against a repo whose
source is Sisyphus itself can lead a worker to stumble on the *old*
control-plane tool names (`mcp_server.py`'s `sisyphus_get_card`, etc.) while
exploring, and hallucinate a double-prefixed tool call. The prompt
(`core/worker.py::_build_prompt`) explicitly warns the model off anything
"sisyphus"-looking that isn't in its declared tool list, but this failure
mode is easiest to reproduce by testing against Sisyphus's own source and
mostly irrelevant to a normal target repo.

## Consequences

- **Gain over ADR 0001:** far less to hold in your head — one status enum,
  one message-posting verb, no journal/activity/comments split, no phase
  dimension. Easier to extend (a new role is a persona + tool list, not a
  new lifecycle state).
- **Cost:** the orchestrator can still thrash on a genuinely broken
  worker/config. Two independent guards catch this: a verbatim-repeat breaker
  (`_consecutive_repeat`, comparing full `"[action] why"` text so legitimate
  engineer/reviewer alternation isn't falsely flagged) and a semantic
  no-result escalation ladder (counting consecutive worker_finished endings
  that delivered nothing, persisted per task per role): at 2 in a row the next
  attempt moves to the NEXT runner option of the role's model class, and at 3
  the task parks with one message to the human naming the failures and each
  run's last output — well short of the 25-pass budget ceiling.

## What's actually done vs. not

1. ✅ Store (`core/store.py`) — tasks + thread.
2. ✅ Agent-facing MCP (`web/agent_mcp.py`) + worker execution
   (`core/worker.py`) — proven live end-to-end (2026-07-25): an explorer and
   a reviewer both correctly called their tools, and the reviewer caught a
   real mistake in the explorer's answer.
3. ✅ Orchestrator (`core/orchestrator.py`) — decision loop, both thrash
   guards, boot-time reconciliation.
4. ⬜ Surfaces — web UI and `/api/v1` endpoints for tasks/threads; the
   existing `mcp_server.py`/CLI still front the old pipeline.
5. ⬜ Retire the old pipeline/FSM/events/resources machinery once the new
   system has been used for real.
