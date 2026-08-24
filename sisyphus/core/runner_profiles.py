"""Per-runner-type launch profiles — what differs between one agent CLI and another.

A runner row carries a ``type`` (see core.runners.RUNNER_TYPES), but until now
core.launcher hardcoded opencode's argv shape *and* OpenRouter's credential
variables for every row regardless of type. A row of any other type could be
created through the UI and would then be fed opencode's flags, so it could
never actually run. This module is the seam: each profile owns exactly the four
things that differ per CLI, and the launcher/worker stay type-agnostic.

  argv        how prompt, model, and working directory reach the binary
  env         which credential variables that CLI actually reads
  tool_prefix how the model must spell our agent-MCP tools in a tool call
  config      the on-disk file that points the CLI at our MCP bridge

Adding a third runner is a new entry here, not an edit to the launcher.

Note on isolation: both supported CLIs load per-user global configuration that
can hijack a run (opencode via its global plugin, Claude Code via user-scoped
settings, hooks and MCP servers). Each profile is responsible for suppressing
that — see the per-profile comments, because the mechanism and its caveats are
different in each.
"""
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Runner types this module knows how to launch. core.runners validates new
# rows against this, so the UI can only offer types that actually run.
SUPPORTED_TYPES = ("opencode", "claude-cli")

DEFAULT_TYPE = "opencode"


class UnsupportedRunner(ValueError):
    """Raised for a runner row whose type has no launch profile."""


def normalize_type(runner: dict) -> str:
    """The profile key for *runner*, defaulting to opencode for a blank type."""
    rtype = ((runner or {}).get("type") or "").strip() or DEFAULT_TYPE
    if rtype not in SUPPORTED_TYPES:
        raise UnsupportedRunner(
            f"runner type {rtype!r} has no launch profile; supported: "
            f"{', '.join(SUPPORTED_TYPES)}"
        )
    return rtype


# ── UI / autodetect defaults ────────────────────────────────────────────

# What a new row of each type should be pre-filled with. Lives here, next to
# the code that consumes these fields at launch time, because the autodetect
# panel used to hardcode OPENROUTER_API_KEY for *every* binary it found — which
# for a Claude row is not merely useless but wrong: build_env would forward an
# OpenRouter key as ANTHROPIC_API_KEY.
_SUGGESTED = {
    "opencode": {
        # Blank, not OpenRouter. opencode picks the provider from the model's
        # own prefix (`deepseek/…` goes straight to DeepSeek, `openrouter/…`
        # through OpenRouter) and authenticates from its own login store, so
        # naming one provider's key and base URL here is wrong for every model
        # that isn't routed through that provider — which was the case for the
        # seeded default runner, whose OpenRouter fields did nothing.
        "api_key_env": "",
        "base_url": "",
        "model": "",
        "hint": "Provider comes from the model prefix (e.g. deepseek/… or openrouter/…); "
                "credentials come from `opencode auth login`.",
    },
    "claude-cli": {
        # Deliberately blank: Claude Code authenticates by OAuth subscription
        # from the keychain as well as by key, and naming a variable that isn't
        # set makes health_check report a healthy runner as unhealthy.
        "api_key_env": "",
        "base_url": "",
        # Blank means "whatever the CLI is configured to use" — no --model flag.
        "model": "",
        "hint": "Uses your existing Claude Code login (OAuth or ANTHROPIC_API_KEY).",
    },
}


def suggested_config(rtype: str) -> dict:
    """Sensible defaults for a new runner row of *rtype*, for the UI to prefill."""
    return dict(_SUGGESTED.get(rtype, _SUGGESTED[DEFAULT_TYPE]))


# Where each CLI keeps its own credentials. Both supported runners log in for
# themselves and read that login at run time, so an unset api_key_env is NOT
# by itself a broken runner — the daemon's environment is nearly empty under
# launchd (see core.launcher), while the CLI's own store is always readable.
# Treating the missing variable as fatal reported working runtimes as
# unhealthy; health_check uses this to say so as a note instead.
_CREDENTIAL_STORE = {
    "opencode": "opencode's own login (~/.local/share/opencode/auth.json — `opencode auth list`)",
    "claude-cli": "your Claude Code login (OAuth in the keychain, or ANTHROPIC_API_KEY)",
}


def credential_store(rtype: str) -> Optional[str]:
    """Human description of where *rtype* finds credentials without an env var,
    or None if that CLI can only authenticate from the environment."""
    return _CREDENTIAL_STORE.get(rtype)


# ── what each CLI leaves in, and reads from, a working tree ─────────────

# Per type: (path, human label) for the files and directories that CLI owns
# inside a repo working tree — its generated config and its own session state.
# Two consumers, both of which used to hardcode opencode's names and so knew
# nothing about any other runner:
#
#   core.git_ops   excludes these from an engineer's commits, so a worker
#                  cannot commit the daemon's own scaffolding into the user's
#                  repo. A Claude worker's cwd is the worktree exactly like
#                  opencode's, so without .claude here its state directory was
#                  one `git add -A` away from landing in a real commit.
#   core.repo_context surfaces them as recognised config when scanning a repo.
_WORKSPACE_ARTIFACTS = {
    "opencode": (
        ("opencode.json", "opencode config"),
        (".opencode", "opencode config dir"),
    ),
    "claude-cli": (
        (".claude", "Claude Code config dir"),
    ),
}


def workspace_artifacts(rtype: Optional[str] = None) -> List[str]:
    """Paths *rtype* owns inside a working tree; every runner's when rtype is
    None — which is what callers computing a diff need, since by then nothing
    records which CLI produced the tree."""
    if rtype is not None:
        return [p for p, _ in _WORKSPACE_ARTIFACTS.get(rtype, ())]
    seen, out = set(), []
    for entries in _WORKSPACE_ARTIFACTS.values():
        for path, _ in entries:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def workspace_artifact_labels() -> List[tuple]:
    """(path, label) for every runner's artifacts, for UI/context listings."""
    seen, out = set(), []
    for entries in _WORKSPACE_ARTIFACTS.values():
        for path, label in entries:
            if path not in seen:
                seen.add(path)
                out.append((path, label))
    return out


# ── tool naming ─────────────────────────────────────────────────────────

def tool_prefix(rtype: str, server_name: str) -> str:
    """How the model addresses a tool from our MCP server, by CLI convention.

    opencode exposes ``<server>_<tool>``; Claude Code exposes
    ``mcp__<server>__<tool>``. The prompt names these tools literally (see
    core.worker._build_prompt for why), so getting this wrong means the model
    is told to call a tool that does not exist under that name.
    """
    if rtype == "claude-cli":
        return f"mcp__{server_name}__"
    return f"{server_name}_"


# ── MCP wiring ──────────────────────────────────────────────────────────

PROBE_TIMEOUT = 20


def foreign_mcp_servers(binary: str) -> List[str]:
    """Every MCP server opencode would merge into a run from OUTSIDE the
    project — the user's own machine.

    A live run's roster carried figma, playwright, macos-use, engram, websearch,
    context7, grep_app and android, because opencode merges the user's global
    configuration into every run (docs/known-gaps.md). That is surface — an
    agent that can drive the user's browser or Figma — and it is spend.

    Asked rather than assumed: `opencode debug config` prints the resolved
    configuration, so this reads what THIS binary will actually load instead of
    guessing at config file locations. Probed from an empty directory, so what
    comes back is only what arrives from outside a project — a repo's own
    opencode.json is the repo's business, not a leak.

    Neither OPENCODE_CONFIG_DIR nor OPENCODE_CONFIG suppresses those entries
    (both were measured: the global servers survive both). A project-level
    ``{"enabled": false}`` does, for local and remote servers alike — which is
    why this returns names for the project config to disable rather than an
    environment switch to set.

    Deliberately not cached: the probe costs under a second against a run that
    lasts minutes, and a cache would keep a newly added global server invisible
    until the daemon restarts. Any failure returns nothing — a run must never
    fail because we could not enumerate what to disable.
    """
    import subprocess
    import tempfile

    if not binary:
        return []
    probe_dir = tempfile.mkdtemp(prefix="sisyphus-cfgprobe-")
    try:
        # Captured to a FILE, not a pipe. The resolved config runs past 64KB and
        # opencode exits before a pipe drains — through capture_output the JSON
        # comes back cut mid-string every time, which parses as a probe failure
        # and silently leaves the whole roster enabled. A file write is
        # synchronous and comes back whole.
        out_path = os.path.join(probe_dir, "resolved.json")
        with open(out_path, "w") as out:
            r = subprocess.run([binary, "debug", "config"], cwd=probe_dir,
                               stdout=out, stderr=subprocess.DEVNULL,
                               timeout=PROBE_TIMEOUT)
        if r.returncode != 0:
            logger.warning("opencode config probe exited %s — leaving the global "
                           "MCP roster in place", r.returncode)
            return []
        with open(out_path) as fh:
            mcp = (json.load(fh) or {}).get("mcp") or {}
        return sorted(str(k) for k in mcp)
    except Exception:  # noqa: BLE001
        logger.warning("could not probe %r for its global MCP roster — leaving it "
                       "in place", binary, exc_info=True)
        return []
    finally:
        import shutil
        shutil.rmtree(probe_dir, ignore_errors=True)


def write_mcp_config(
    rtype: str,
    *,
    worktree_path: str,
    output_dir: str,
    server_name: str,
    command: List[str],
    env: dict,
    agent_name: str,
    model: str = "",
    binary: str = "",
) -> Optional[str]:
    """Write the CLI's config so it launches our stdio MCP bridge.

    Returns a path to pass back as ``mcp_config`` (Claude Code takes it as a
    flag), or None when the CLI discovers the config from the working
    directory on its own (opencode).
    """
    if rtype == "claude-cli":
        # Written under the run's output dir, NOT the worktree: the worktree is
        # a real git checkout whose porcelain status is read for diffing and
        # for salvaging uncommitted work (core.worker.cleanup_task._salvage),
        # so dropping another untracked file in there adds noise to every run's
        # diff. Claude Code takes an absolute path, so nothing requires it to
        # sit in the cwd.
        path = Path(output_dir) / "mcp-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "mcpServers": {
                server_name: {
                    "command": command[0],
                    "args": list(command[1:]),
                    "env": dict(env),
                }
            }
        }, indent=2))
        return str(path)

    # opencode: a project-level opencode.json in its cwd. Role restriction is
    # enforced server-side (by the session token), so this needs no tool
    # allowlist.
    #
    # "plugin": [] was believed to override the user's global plugin list. A live
    # roster says otherwise: a worker asked to name its own tools reported
    # call_omo_agent, skill_mcp, session_* and ast_grep_* with this array empty
    # AND every OPENCODE_DISABLE_* switch set (task-2ff103e9, docs/known-gaps).
    # It stays because it is the documented way to say "no plugins" and costs
    # nothing, but do not read it as isolation — that gap is open and measured.
    # What the array does still buy: our own tools reach the model, so the
    # skill_mcp indirection that once swallowed them is not doing so now.
    # --pure was tried first, but confirmed by live probing (opencode debug
    # logs + a direct tool-list run) to strip project-local MCP servers too —
    # with --pure our own server never even connects.
    #
    # "agent": {<agent_name>: {"mode": "primary"}} defines our OWN agent
    # entirely within project config, instead of asking opencode to run one of
    # its built-in agents by name (originally "build"). That worked at first
    # but broke with no change on our side: the global oh-my-openagent config
    # can (and on this machine later did) reclassify a built-in agent name as a
    # subagent, at which point opencode silently falls back to its own default
    # — the very persona we were avoiding, reintroducing the skill_mcp
    # indirection bug. A name we mint and define ourselves, with "mode" set
    # explicitly, isn't subject to that.
    # The agent MUST carry an explicit model. A primary agent defined without
    # one makes `opencode run` hang forever with no stdout and no stderr —
    # confirmed by bisecting the generated config: the same config with a
    # "model" key finishes in 5s, without it the process never produces a byte
    # and is eventually killed by the launcher's idle timeout. Upstream appears
    # to fall back to an interactive model picker, which in a non-interactive
    # run nothing can answer. The symptom reaches the user as "decider produced
    # no decision", which points nowhere near the real cause.
    agent_def: dict = {"mode": "primary"}
    if model:
        agent_def["model"] = model
    else:
        logger.warning(
            "opencode agent %r is being written with no model — `opencode run` "
            "hangs on a primary agent that does not name one", agent_name,
        )

    # Disable, by name, every MCP server the user's global config would merge
    # in. A bare {"enabled": false} is enough — measured against both local and
    # remote entries — and it is deliberately bare: copying the original entry
    # to flip a flag would write the user's own server URLs and environment
    # (credentials included) into a file inside the worktree, where the agent
    # reads it. The stub says "off" and nothing else.
    mcp: dict = {
        name: {"enabled": False}
        for name in foreign_mcp_servers(binary) if name != server_name
    }
    mcp[server_name] = {
        "type": "local",
        "command": list(command),
        "environment": dict(env),
        "enabled": True,
    }
    config = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "agent": {agent_name: agent_def},
        "mcp": mcp,
    }
    Path(worktree_path, "opencode.json").write_text(json.dumps(config, indent=2))
    return None


# ── argv ────────────────────────────────────────────────────────────────

def build_argv(
    rtype: str,
    binary: str,
    extra_args: List[str],
    prompt: str,
    *,
    worktree_path: str = "",
    pure: bool = False,
    agent: Optional[str] = None,
    model: str = "",
    mcp_config: Optional[str] = None,
) -> List[str]:
    """The full command line for one non-interactive run of this CLI."""
    if rtype == "claude-cli":
        return _claude_argv(binary, extra_args, prompt, model=model,
                            mcp_config=mcp_config)
    return _opencode_argv(binary, extra_args, prompt, worktree_path=worktree_path,
                          pure=pure, agent=agent)


def _opencode_argv(binary, extra_args, prompt, *, worktree_path, pure, agent):
    args = [binary, "run"] + list(extra_args) + [prompt, "--format", "json", "--auto"]
    if pure:
        args.append("--pure")
    if agent:
        args.extend(["--agent", agent])
    if worktree_path and worktree_path != "/tmp":
        args.extend(["--dir", str(worktree_path)])
    return args


def _claude_argv(binary, extra_args, prompt, *, model, mcp_config):
    """Claude Code in non-interactive (-p) mode.

    Flag choices worth keeping:

    --output-format stream-json (with the --verbose the CLI hard-errors
    without): NOT the plain "json" format, even though a single object would be
    easier to parse. Plain json buffers everything and writes it in one burst
    when the process exits — measured here: first byte and last byte both at
    10.8s, nothing before. The launcher kills a run that has produced no output
    for idle_timeout seconds (300 by default, see core.worker), so a plain-json
    worker would be killed the moment real work took longer than five minutes
    of silence, while short runs passed — the worst possible failure shape.
    stream-json emits an event per step (measured: 12 chunks, longest silence
    3.5s), which keeps the idle timer honest and still ends with the same
    `"type":"result"` object that carries the run's usage.

    --permission-mode bypassPermissions: in -p mode nobody can answer a
    permission prompt, so any gated tool call would hang until the launcher's
    idle timeout kills the run. The worker already executes inside the Seatbelt
    sandbox (core.sandbox), which is where write confinement actually comes
    from — the CLI's own prompting adds nothing here but a deadlock.

    --strict-mcp-config: use ONLY the server we pass in, ignoring the user's
    global and project MCP registrations. The opencode side of this codebase
    learned the hard way that an unrelated globally-registered server named
    "sisyphus" can win a name collision and leave the model with none of our
    tools (see core.worker._MCP_SERVER_NAME); this flag removes that whole
    class of failure rather than working around it.

    --setting-sources project: skip the user's own settings, hooks and
    auto-memory, which would otherwise run inside every worker. Deliberately
    NOT --bare, which achieves the same isolation but forces API-key-only auth
    and so breaks any machine authenticated by OAuth subscription rather than
    ANTHROPIC_API_KEY (this one, at the time of writing).
    """
    args = [binary, "-p"] + list(extra_args) + [
        prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "--setting-sources", "project",
    ]
    if mcp_config:
        args.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])
    if model:
        args.extend(["--model", model])
    return args


# ── environment ─────────────────────────────────────────────────────────

def build_env(rtype: str, runner: dict) -> dict:
    """Credential/model variables to overlay onto the worker's environment.

    Only the variables the target CLI actually reads — the launcher previously
    exported OPENROUTER_* unconditionally, which is meaningless to a CLI that
    talks to Anthropic directly and would silently mislead anyone debugging a
    failed run by its environment.
    """
    overlay: dict = {}
    api_env = (runner.get("api_key_env") or "").strip()
    key = os.environ.get(api_env, "") if api_env else ""
    base_url = (runner.get("base_url") or "").strip()

    if rtype == "claude-cli":
        # No key configured is a valid, common setup: Claude Code also
        # authenticates via an OAuth subscription held in the keychain.
        if key:
            overlay[api_env] = key
            overlay.setdefault("ANTHROPIC_API_KEY", key)
        if base_url:
            overlay["ANTHROPIC_BASE_URL"] = base_url
        return overlay

    if key:
        overlay[api_env] = key
        overlay["OPENROUTER_API_KEY"] = key
    if base_url:
        overlay["OPENROUTER_BASE_URL"] = base_url
    # Trim the tool surface the user's own machine leaks into a worker: a live
    # run's roster showed figma, playwright, macos-use and the oh-my-openagent
    # plugin suite — the user's GLOBAL opencode config merges into every run.
    # These switches come from opencode's own help text and kill the plugin and
    # external-skill share of that; globally configured MCP servers are a
    # separate leak these do not close (docs/known-gaps.md).
    overlay["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
    overlay["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    overlay["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "1"
    return overlay
