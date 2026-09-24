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
from collections import namedtuple
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Credential/secret dirs a worker never has a reason to write — protected in
# addition to the repo sources, as cheap defense-in-depth.
_SECRET_SUBPATHS = (".ssh", ".aws", ".gnupg", ".config/gh", ".config/gcloud", ".kube")

# Paths under $HOME that a worker legitimately needs to READ when read_isolation
# is enabled. Each entry is (path, reason_why_it_is_needed). This allowlist is
# re-opened after the home directory read-deny is applied, so only these paths
# and $WORKSPACE_ROOT, $WORKTREE, and $OUTPUT_DIR (paths already in the profile)
# become readable. Anything outside $HOME stays readable (system tools, Homebrew,
# etc.). The runner's private HOME from core.runner_home is built under the
# workspace, so it is automatically re-allowed.
_ReadAllowedPath = namedtuple("ReadAllowedPath", ["path", "reason"])
_READ_ALLOWLIST = [
    _ReadAllowedPath(".local/share/opencode", "opencode CLI credentials and login store"),
    _ReadAllowedPath(".local/bin", "user's custom scripts installed by package managers"),
    _ReadAllowedPath(".local/state", "XDG state dir for opencode and other CLI tools"),
    _ReadAllowedPath(".opencode", "opencode legacy state directory"),
    # Only the wrapper's bin: the rest of ~/.superset is another tool's state.
    _ReadAllowedPath(".superset/bin", "the `claude` wrapper some PATHs resolve first"),
    # Named subdirs, never all of ~/.config: it holds ~/.config/gh/hosts.yml
    # (a GitHub token) and every other CLI's credentials.
    _ReadAllowedPath(".config/opencode", "opencode's own config (plugins are stripped per run)"),
    _ReadAllowedPath(".config/git", "git's XDG config and ignore file"),
    _ReadAllowedPath(".claude", "Claude Code state and authentication"),
    _ReadAllowedPath(".claude.json", "Claude Code configuration"),
    _ReadAllowedPath(".cache", "caches from build tools and CLIs"),
    _ReadAllowedPath(".npm", "npm package cache"),
    _ReadAllowedPath(".bun", "bun package manager cache and store"),
    _ReadAllowedPath("Library/Keychains", "macOS Keychain (Claude Code login is here)"),
    _ReadAllowedPath("Library/Caches", "macOS system caches for CLIs and build tools"),
    _ReadAllowedPath(".gitconfig", "git configuration and user identity"),
]


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


def _read_isolation_enabled() -> bool:
    """Whether opt-in read isolation for the home directory is enabled.

    Read from the config (sandbox.read_isolation) with the same pattern as
    sandbox.require — environment variable override is possible but not needed
    since this is an opt-in knob, not a safety feature. Default is False (off)."""
    try:
        from core.config import get_config
        return bool(get_config("sandbox.read_isolation", False))
    except Exception:  # noqa: BLE001
        logger.exception("could not read sandbox.read_isolation — defaulting to off")
        return False


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


def _read_isolation_paths(home: str):
    """The paths under *home* re-opened for reading when read_isolation is on,
    or None when it is off. Missing optional paths are left out, so an empty
    list is a real answer: deny the home and re-open none of it."""
    if not home or not _read_isolation_enabled():
        return None
    return [str(Path(home) / e.path) for e in _READ_ALLOWLIST
            if os.path.exists(Path(home) / e.path)]


def _daemon_state_paths(db_path: Optional[str] = None) -> List[str]:
    """SBPL rules for the daemon's own state: the live database (with its WAL
    and shared-memory sidecars, which are as good as the database) and the
    backup snapshots. Literals for the database files so a worker can still
    write its own scratch files beside them, a subpath for the backups dir."""
    if db_path is None:
        from core.db import DB_PATH
        db_path = DB_PATH

    out: List[str] = []
    if db_path:
        out += [f"(literal {_q(db_path + suffix)})" for suffix in ("", "-wal", "-shm", "-journal")]
        backups = str(Path(db_path).parent / "backups")
        out.append(f"(subpath {_q(backups)})")
    return out


def build_seatbelt_profile(worktree: str, output_dir: str,
                           workspace_root: Optional[str] = None,
                           home: Optional[str] = None,
                           protected: Optional[List[str]] = None,
                           db_path: Optional[str] = None,
                           egress_enabled: bool = False) -> str:
    """Allow-by-default, then deny writes to the protected paths (repo sources +
    credential dirs), then re-allow the workspace areas a worker needs — so a
    path under a protected repo but inside the workspace stays writable (last
    matching rule wins in SBPL), while the repo's real files do not.

    When sandbox.read_isolation is enabled, also deny reads to the home directory
    and re-allow only the specific paths a worker legitimately needs (credentials,
    caches, toolchain state). The worktree and workspace are re-allowed for both
    reads and writes so they remain accessible.

    When egress_enabled, also deny all network-outbound except to localhost,
    forcing the worker through the egress proxy."""
    workspace_root = workspace_root or os.environ.get("WORKSPACE_ROOT", "")
    home = home if home is not None else os.environ.get("HOME", "")
    protected = protected if protected is not None else _protected_paths(home)

    deny = " ".join(f"(subpath {_q(p)})" for p in protected) if protected else ""

    allow_subpaths = [p for p in (workspace_root, worktree, output_dir) if p]
    allow = "\n".join(f"(subpath {_q(p)})" for p in allow_subpaths)

    # Build the profile with SBPL rule ordering explained in comments below.
    # In SBPL, the LAST matching rule wins, so we control behavior by careful
    # ordering of allow/deny clauses.
    lines = ["(version 1)", "(allow default)"]

    # Write access control (file-write*):
    # 1. deny writes to protected paths (repo sources + credential dirs)
    # 2. re-allow writes to workspace areas (which may be inside protected repos)
    # 3. (later) re-deny writes to the daemon's own database
    # Network egress control: deny outbound except to localhost.
    # This forces the worker through the HTTP CONNECT proxy for any external
    # connections, which will check against the allowlist.
    if egress_enabled:
        # SBPL wants host:port filters; a bare address is a profile error
        # ("port missing in network address"), which sandbox-exec reports and
        # then refuses to run anything. localhost:* covers the proxy and the
        # daemon's MCP endpoint; unix sockets stay open for the system daemons
        # (DNS, keychain) every process talks to.
        lines.append("(deny network-outbound)")
        lines.append("(allow network-outbound (remote ip \"localhost:*\") (remote unix-socket))")

    if deny:
        lines.append(f"(deny file-write* {deny})")
    if allow:
        lines.append("(allow file-write*\n" + allow + "\n)")

    # Read access control (file-read*) — only if read_isolation is on:
    # The workspace paths MUST be re-allowed for reads here, even though they
    # are allowed by default, because the read-deny below will match them
    # otherwise. The re-allow rules are evaluated AFTER the deny, so they
    # override it (last matching rule wins).
    # Deny first, then re-open (the last matching rule wins in SBPL): the
    # allowlisted home paths, and the workspace areas the read-deny would
    # otherwise catch when the workspace lives under $HOME.
    readable = _read_isolation_paths(home)
    if readable is not None:
        lines.append(f"(deny file-read* (subpath {_q(home)}))")
        # Sisyphus's own tools/ holds the stdio MCP bridge every worker runs
        # (stdlib only) — without it Claude Code starts with no tools at all.
        # Only tools/, never the install dir: .env sits next to it.
        tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
        lines.append("(allow file-read*\n"
                     + "\n".join(f"(subpath {_q(p)})"
                                  for p in readable + allow_subpaths + [tools_dir])
                     + "\n)")
        # Metadata (stat) stays allowed across the home: runners walk up from
        # their cwd looking for config and resolve symlinks on the way, and
        # opencode died on the first denied lstat. Metadata is not content —
        # readdir is a data read, so a denied directory cannot even be listed;
        # what leaks is whether a path someone already knows exists.
        lines.append(f"(allow file-read-metadata (subpath {_q(home)}))")

    # Database protection — MUST BE LAST, because the workspace allow above
    # re-opens everything under it — and the daemon's own database lives there.
    # Nothing inside the sandbox has any business writing it: a worker reaches
    # the daemon over its HTTP/MCP bridge, never through sqlite. The Python-level
    # guard (SISYPHUS_DB_PROTECT, core/db.py) only governs connections core.db
    # opens, so `sqlite3 <path>` from the agent's bash tool walked straight past
    # it and could corrupt the one record of every task. Same for the snapshots
    # that would undo it.
    keep_out = _daemon_state_paths(db_path)
    if keep_out:
        lines.append("(deny file-write*\n"
                     + "\n".join(keep_out) + "\n)")
    return "\n".join(lines) + "\n"


def _seatbelt_wrap(args: List[str], worktree: str, output_dir: str) -> List[str]:
    from core.config import get_config
    egress_enabled = get_config("sandbox.egress", False)
    profile = build_seatbelt_profile(worktree, output_dir, egress_enabled=egress_enabled)
    profile_path = Path(output_dir) / "sandbox.sb"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile)
    return [_SANDBOX_EXEC, "-f", str(profile_path), *args]
