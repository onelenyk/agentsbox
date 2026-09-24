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


# ── worker timeouts ─────────────────────────────────────────────────────

# (idle, total) seconds a WORKER run of each type lives under, before the
# launcher abandons it. idle is silence-with-no-output; total is wall-clock.
# A slow model (opencode + deepseek thinking for minutes without streaming)
# used to be killed by the old flat 300s idle limit (core.worker
# WORKER_IDLE_TIMEOUT_SECONDS) while its run was still genuinely working —
# 10 of 127 worker runs died exactly that way. opencode gets twice the idle
# allowance for exactly that reason; claude-cli streams per step (its
# --output-format stream-json), so its real silence never gets close to 300s.
# A runner row's own idle_timeout/timeout columns override these (blank = this).
# The total is the same 1800s for both — a run's budget, not a per-type knob.
TIMEOUT_DEFAULTS = {
    "opencode": (600, 1800),
    "claude-cli": (300, 1800),
}
# Fallback for a type with no recorded defaults — mirrors the module constants
# in core.worker (WORKER_IDLE_TIMEOUT_SECONDS / WORKER_TIMEOUT_SECONDS). Kept
# in lockstep there; effective_timeouts is what both the launcher wiring and
# the Daemon page read.
FALLBACK_TIMEOUTS = (300, 1800)


def _positive_timeout(value) -> Optional[int]:
    """*value* as a positive int, or None when it is not set. The form already
    rejects junk; this merely keeps a bad row (hand-edited DB) from asserting."""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def effective_timeouts(runner: dict) -> tuple:
    """(idle, total) seconds a worker run of *runner* actually runs under.

    The runner row's own ``idle_timeout``/``timeout`` columns win when set;
    otherwise the type's defaults (TIMEOUT_DEFAULTS); otherwise the platform
    fallback (core.worker's constants). The two are resolved independently, so
    a row can raise just the idle allowance while keeping the total default.
    """
    idle_default, total_default = TIMEOUT_DEFAULTS.get(
        ((runner or {}).get("type") or "").strip() or DEFAULT_TYPE,
        FALLBACK_TIMEOUTS)
    idle = _positive_timeout((runner or {}).get("idle_timeout"))
    total = _positive_timeout((runner or {}).get("timeout"))
    return (idle or idle_default, total or total_default)


# ── CLI versions this profile was written against ───────────────────────

# Every flag in build_argv below is this project's reading of a CLI it does not
# own: `run --format json --auto --pure --agent --dir` for opencode, the
# stream-json pair for claude-cli. A CLI that renames one of them, or changes
# the shape of what --format json prints, breaks every run — and it breaks them
# at the worst moment, in a worker nobody is watching, with an error that looks
# like the model misbehaving.
#
# So the versions that were actually exercised are written down. A different
# version is NOT an error (these CLIs update constantly and almost every update
# is harmless); it is a note on the runner's health, so that when runs start
# failing strangely there is somewhere that says "this is not the version this
# was tested against". Update the value after running the suite against the new
# CLI.
VERIFIED_VERSIONS = {
    "opencode": "1.18.31",
    "claude-cli": "2.1.278",
}


def parse_version(out: str) -> str:
    """The version number in a CLI's `--version` output. opencode prints a bare
    "1.18.31"; claude-cli prints "2.1.278 (Claude Code)". Anything else yields
    "" — an unreadable version is not worth guessing at."""
    import re
    m = re.search(r"\d+\.\d+(?:\.\d+)?", out or "")
    return m.group(0) if m else ""


def version_note(rtype: str, version: str) -> Optional[str]:
    """What health should say about *version*, or None when there is nothing to
    say (it matches, or we could not read it, or this type has no record)."""
    verified = VERIFIED_VERSIONS.get(rtype)
    if not verified or not version or version == verified:
        return None
    return (f"{rtype} {version} is installed; this daemon's launch flags were "
            f"verified against {verified}")


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
_FOREIGN_CACHE: dict = {}


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
    # Probe under the HOME the RUN will use, not the daemon's. Since worker runs
    # got a home of their own, probing with the daemon's home reported the
    # user's eight global servers and wrote eight stubs disabling things that
    # were never going to be there — dead weight that reads as protection. The
    # mechanism stays: it is the belt to the private home's braces, and it now
    # measures the same environment the worker gets.
    from .runner_home import build as _build_home
    home = _build_home(os.environ.get("WORKSPACE_ROOT", ""))

    # Nothing to find, and asking costs 12 seconds: spawning the binary against
    # a home it has never seen makes it bootstrap its package cache first. The
    # allowlist in core.runner_home links no config dir, so normally there is
    # none. That is not a guarantee — opencode CREATES one when something asks
    # it to install a plugin (docs/known-gaps.md: a repo's own .opencode config
    # can) — which is precisely why this checks rather than assumes.
    if not os.path.isdir(os.path.join(home, ".config", "opencode")):
        return []

    # Cacheable now, and it was not before. While this probed the DAEMON's home
    # it was reading the user's own config, which they can change under us at
    # any moment; under a home we assemble ourselves the answer only changes
    # when we change it.
    cached = _FOREIGN_CACHE.get((binary, home))
    if cached is not None:
        return list(cached)

    env = dict(os.environ)
    env["HOME"] = home
    probe_dir = tempfile.mkdtemp(prefix="sisyphus-cfgprobe-")
    try:
        # Captured to a FILE, not a pipe. The resolved config runs past 64KB and
        # opencode exits before a pipe drains — through capture_output the JSON
        # comes back cut mid-string every time, which parses as a probe failure
        # and silently leaves the whole roster enabled. A file write is
        # synchronous and comes back whole.
        out_path = os.path.join(probe_dir, "resolved.json")
        with open(out_path, "w") as out:
            r = subprocess.run([binary, "debug", "config"], cwd=probe_dir, env=env,
                               stdout=out, stderr=subprocess.DEVNULL,
                               timeout=PROBE_TIMEOUT)
        if r.returncode != 0:
            logger.warning("opencode config probe exited %s — leaving the global "
                           "MCP roster in place", r.returncode)
            return []
        with open(out_path) as fh:
            mcp = (json.load(fh) or {}).get("mcp") or {}
        found = sorted(str(k) for k in mcp)
        _FOREIGN_CACHE[(binary, home)] = found
        return list(found)
    except Exception:  # noqa: BLE001
        logger.warning("could not probe %r for its global MCP roster — leaving it "
                       "in place", binary, exc_info=True)
        return []
    finally:
        import shutil
        shutil.rmtree(probe_dir, ignore_errors=True)


def repo_declared_opencode(worktree_path: str) -> dict:
    """What the checkout's own opencode config files declare — read them
    BEFORE write_mcp_config overwrites the winning slot.

    A repo may carry opencode.json at its root and/or .opencode/opencode.json,
    and opencode merges both into a run (docs/known-gaps.md, measured
    2026-08-25): `mcp` entries merge from either file no matter where ours
    sits, so a server named there still appears unless disabled by name —
    which is what the caller does with these names. `plugin` follows file
    precedence and our file occupies the winning slot, so the list here is
    reported rather than fought. Unparseable files are reported too: a config
    we could not read is a fact about the checkout, not a reason to guess.
    """
    out = {"mcp": [], "plugin": [], "files": []}
    for rel in ("opencode.json", ".opencode/opencode.json"):
        p = Path(worktree_path, rel)
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text())
        except Exception:  # noqa: BLE001 — unreadable is still worth reporting
            out["files"].append(f"{rel} (unparseable)")
            continue
        out["files"].append(rel)
        for name in (data.get("mcp") or {}):
            if name not in out["mcp"]:
                out["mcp"].append(str(name))
        for plug in (data.get("plugin") or []):
            if plug not in out["plugin"]:
                out["plugin"].append(str(plug))
    return out


def _mark_skip_worktree(worktree_path: str, rel: str) -> None:
    """If *rel* is TRACKED in this checkout, flag it skip-worktree so our
    overwrite never shows up as a dirty file — not in status, not in a diff,
    not stageable even by a raw `git add` the persona shells out to. The
    sanctioned commit path already excludes it by pathspec (core.git_ops);
    this covers the unsanctioned one. Best-effort: outside a git checkout
    (a scratch dir, a unit test's tmp_path) there is nothing to mark."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", worktree_path, "ls-files", "--error-unmatch", rel],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            subprocess.run(["git", "-C", worktree_path, "update-index", "--skip-worktree", rel],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a guard that cannot run must not stop the run
        logger.debug("could not mark %s skip-worktree in %s", rel, worktree_path)


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
    # The cause is known and is not a setting we can pass: the plugin is
    # installed into opencode's own package registry (~/.cache/opencode/
    # packages/package.json) and loads from there, immune to every config lever
    # tried — this array, the DISABLE switches, --pure, OPENCODE_CONFIG(_DIR),
    # XDG_CONFIG_HOME and XDG_CACHE_HOME alike. Accepted rather than fought:
    # it is the owner's own plugin and they want it (docs/known-gaps.md).
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
    declared = repo_declared_opencode(worktree_path)
    mcp: dict = {
        name: {"enabled": False}
        for name in foreign_mcp_servers(binary) if name != server_name
    }
    # ...and every server the CHECKOUT's own config declares. `mcp` entries
    # merge across config files regardless of which one wins (measured,
    # docs/known-gaps.md), so occupying the winning slot below is not enough
    # on its own — a hostile .opencode/opencode.json in the checkout would
    # still put its server on the roster. Disable-by-name is the mechanism
    # that works from either file.
    for name in declared["mcp"]:
        if name != server_name:
            mcp[name] = {"enabled": False}
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
    # The winning slot. Measured precedence (docs/known-gaps.md, 2026-08-25):
    # with our config in .opencode/ and a repo's at the worktree root, ours
    # wins on `plugin`; the reverse loses. And when the repo ships its own
    # .opencode/opencode.json, this write REPLACES it in the throwaway
    # checkout — its plugin list never reaches opencode at all. What the
    # replaced file declared is reported by repo_declared_opencode above, and
    # the worker posts it to the task thread: overridden loudly, not silently.
    # (.opencode subfiles beyond opencode.json — agent/*.md and the like —
    # remain unmeasured; the config file is what the 08-25 measurements cover.)
    slot = Path(worktree_path, ".opencode", "opencode.json")
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps(config, indent=2))
    _mark_skip_worktree(worktree_path, ".opencode/opencode.json")
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

# Variables that would override a CLI's own sign-in. Claude Code prefers an
# ANTHROPIC_API_KEY in its environment over the OAuth login in the keychain, so
# a daemon started from a shell that happens to export one would bill that key
# for a runner the owner set to "use its own login".
_LOGIN_OVERRIDES = {
    "claude-cli": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
}


# Runners that get the owner's real HOME instead of core.runner_home's private
# one. The private home exists because opencode reads its config from $HOME
# ahead of every flag, so HOME is the only lever on its tool roster. Claude Code
# has real levers — --strict-mcp-config and --setting-sources project (see
# _claude_argv) — and on macOS its login lives in the Keychain, which a
# substituted HOME hides: every run answered "Not logged in" while the smoke
# test, run under the real HOME, kept reporting the runner healthy.
_KEEPS_REAL_HOME = ("claude-cli",)


def worker_home(rtype: str, root: str) -> Optional[str]:
    """The HOME a run of *rtype* gets (private homes live under *root*), or
    None to keep the daemon's own.

    The one rule both the launcher and the smoke test apply, so a smoke test
    cannot pass under an environment the real run never sees."""
    if rtype in _KEEPS_REAL_HOME:
        return None
    from .runner_home import build as _build_home
    return _build_home(root)


def env_to_drop(rtype: str, runner: dict) -> tuple:
    """Inherited variables to remove before build_env's overlay is applied."""
    from .runners import auth_mode
    return _LOGIN_OVERRIDES.get(rtype, ()) if auth_mode(runner) == "login" else ()


def build_env(rtype: str, runner: dict) -> dict:
    """Credential/model variables to overlay onto the worker's environment.

    Only the variables the target CLI actually reads — the launcher previously
    exported OPENROUTER_* unconditionally, which is meaningless to a CLI that
    talks to Anthropic directly and would silently mislead anyone debugging a
    failed run by its environment.
    """
    from .runners import auth_mode
    overlay: dict = {}
    mode = auth_mode(runner)
    api_env = (runner.get("api_key_env") or "").strip()
    base_url = (runner.get("base_url") or "").strip()
    if mode == "login":
        api_env, key = "", ""
    elif mode == "key":
        from . import keychain
        pasted = keychain.read(runner.get("id") or "") or ""
        if pasted and api_env:
            # A pasted key goes to exactly the variable its provider reads,
            # chosen when it was saved (core.runner_catalog.provider_env) —
            # not also to OPENROUTER_API_KEY, as a variable from the
            # environment is below.
            overlay[api_env] = pasted
        key = ""
    else:
        key = os.environ.get(api_env, "") if api_env else ""

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
