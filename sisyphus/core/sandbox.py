"""Filesystem sandbox for worker subprocesses — a platform-neutral interface
over a swappable enforcement backend.

Worker isolation was soft: the agent's cwd is a git worktree, but opencode's
native tools (bash with an arbitrary workdir, glob/edit on absolute paths) have
full filesystem access — nothing stopped an agent from finding the real source
repo on disk and committing to it (which happened live: 2026-08-04). A sandbox
backend wraps the runner subprocess so the demonstrated escape is refused by the
OS, not just by convention.

`enabled()` / `wrap()` are the stable, platform-neutral contract the launcher
calls. The *enforcement* is inherently platform-specific, so it lives in a
backend:

  - macOS  → Seatbelt via `sandbox-exec` (implemented here).
  - Linux  → bubblewrap / nsjail (future).
  - any    → a Docker/container backend (future, cross-platform).

Policy model (Seatbelt backend): **allow by default, then deny writes to the
things worth protecting** — every registered repo's on-disk source, plus common
credential dirs — and re-allow the workspace (which legitimately lives inside a
repo source). A tight write-allowlist was tried first and abandoned: opencode's
own footprint (its ~/.superset install, a background server, assorted caches) is
large and undocumented, and an allowlist kept breaking it in ways with no
diagnosable denial. A denylist reliably closes the actual hole (writing to a
real repo) without fighting opencode. It is deliberately narrower than a full
jail — an agent can still write to non-repo areas of $HOME — which is the
tradeoff for a native, dependency-free sandbox; the Docker backend is the path
to full confinement. On a platform with no backend, `wrap()` is a no-op (the run
proceeds unconfined) and `enabled()` returns False so callers can warn.
"""
import logging
import os
import platform
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Credential/secret dirs a worker never has a reason to write — protected in
# addition to the repo sources, as cheap defense-in-depth.
_SECRET_SUBPATHS = (".ssh", ".aws", ".gnupg", ".config/gh", ".config/gcloud", ".kube")


# ── platform-neutral contract ───────────────────────────────────────────

def backend() -> Optional[str]:
    """Name of the enforcement backend usable on this host, or None if the
    platform has no sandbox we know how to apply."""
    if platform.system() == "Darwin" and os.path.exists(_SANDBOX_EXEC):
        return "seatbelt"
    return None


def available() -> bool:
    return backend() is not None


def disabled_by_operator() -> bool:
    """Whether SISYPHUS_SANDBOX=0 turned the sandbox off on a host that has one.

    Kept apart from `available()` because the two unconfined states need
    different words: one is a platform we have no backend for, the other is
    somebody switching the guard off. The launcher warned about the first and
    said nothing at all about the second, which is the one worth hearing.
    """
    return os.environ.get("SISYPHUS_SANDBOX", "").strip() in ("0", "false", "off", "no")


def enabled() -> bool:
    """Sandbox is on by default where a backend exists; SISYPHUS_SANDBOX=0
    disables it (an escape hatch for debugging an over-tight profile)."""
    if disabled_by_operator():
        return False
    return available()


def required() -> bool:
    """Whether a run must refuse to start rather than proceed unconfined.

    The default follows the platform: where a backend EXISTS (macOS), losing
    confinement must be a stated decision, so an unset key means required —
    running unconfined then takes two deliberate steps (SISYPHUS_SANDBOX=0
    AND sandbox.require: false), the same shape as every other guardrail
    override here. Where no backend exists yet (Linux), requiring one would
    make the daemon unusable rather than safer, so an unset key means not
    required and the launcher keeps announcing every unconfined run instead.

        sandbox:
          require: false         # or SISYPHUS_SANDBOX_REQUIRE=0

    An unreadable config falls back to that same platform default — on a
    backend host that cannot be called fail-closed, because the sandbox is
    available and runs proceed confined; only an operator who also switched
    the sandbox off is refused, and the env override still works there.
    """
    env = os.environ.get("SISYPHUS_SANDBOX_REQUIRE", "").strip()
    if env:
        return env in ("1", "true", "on", "yes")
    try:
        from core.config import get_config
        val = get_config("sandbox.require", None)
        if val is None:
            return available()
        return bool(val)
    except Exception:  # noqa: BLE001 — an unreadable config keeps the platform default
        logger.exception("could not read sandbox.require — using the platform default")
        return available()


def unconfined_reason() -> Optional[str]:
    """Why this run would not be confined, or None if it would be.

    One sentence, so the launcher, preflight and any future caller all say the
    same thing about the same state instead of each phrasing it their own way.
    """
    if enabled():
        return None
    if disabled_by_operator():
        return ("SISYPHUS_SANDBOX=0 — the filesystem sandbox is switched off, "
                "the worker can write to any repo on this machine")
    return (f"no filesystem sandbox backend on this platform "
            f"({platform.system()}) — the worker runs unconfined")


def wrap(args: List[str], worktree: str, output_dir: str) -> List[str]:
    """Wrap *args* so the subprocess runs under this host's sandbox backend.
    Returns *args* unchanged (a no-op) when the sandbox is disabled or the
    platform has no backend."""
    if not enabled():
        return args
    if backend() == "seatbelt":
        return _seatbelt_wrap(args, worktree, output_dir)
    return args


# ── macOS Seatbelt backend ──────────────────────────────────────────────

def _q(p: str) -> str:
    """A resolved, quoted path for a Seatbelt (subpath ...) clause."""
    resolved = str(Path(p).resolve())
    return '"' + resolved.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _protected_paths(home: str) -> List[str]:
    """Paths a worker must never write: every registered repo's source, plus
    credential dirs. Repo sources come from config so a newly-added repo is
    protected on the next run without touching this code."""
    paths: List[str] = []
    try:
        from core import repos
        for r in repos.list_repos():
            src = (r.get("source") or "").strip()
            if src and os.path.isabs(src):
                paths.append(src)
    except Exception:  # noqa: BLE001 — never let policy-building break a run
        logger.exception("could not enumerate repo sources for the sandbox profile")
    if home:
        paths += [str(Path(home) / s) for s in _SECRET_SUBPATHS]
    # de-dup while preserving order
    seen, out = set(), []
    for p in paths:
        rp = str(Path(p).resolve())
        if rp not in seen:
            seen.add(rp); out.append(p)
    return out


def build_seatbelt_profile(worktree: str, output_dir: str,
                           workspace_root: Optional[str] = None,
                           home: Optional[str] = None,
                           protected: Optional[List[str]] = None) -> str:
    """Allow-by-default, then deny writes to the protected paths (repo sources +
    credential dirs), then re-allow the workspace areas a worker needs — so a
    path under a protected repo but inside the workspace stays writable (last
    matching rule wins in SBPL), while the repo's real files do not."""
    workspace_root = workspace_root or os.environ.get("WORKSPACE_ROOT", "")
    home = home if home is not None else os.environ.get("HOME", "")
    protected = protected if protected is not None else _protected_paths(home)

    deny = " ".join(f"(subpath {_q(p)})" for p in protected) if protected else ""

    allow_subpaths = [p for p in (workspace_root, worktree, output_dir) if p]
    allow = "\n".join(f"(subpath {_q(p)})" for p in allow_subpaths)

    lines = ["(version 1)", "(allow default)"]
    if deny:
        lines.append(f"(deny file-write* {deny})")
    if allow:
        lines.append("(allow file-write*\n" + allow + "\n)")
    return "\n".join(lines) + "\n"


def _seatbelt_wrap(args: List[str], worktree: str, output_dir: str) -> List[str]:
    profile = build_seatbelt_profile(worktree, output_dir)
    profile_path = Path(output_dir) / "sandbox.sb"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile)
    return [_SANDBOX_EXEC, "-f", str(profile_path), *args]
