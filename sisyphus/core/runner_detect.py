"""Auto-detect installed CLI runner binaries.

Resolves a runner's binary to an absolute path by searching PATH plus the common
places CLI tools install to. This makes runners work regardless of the PATH the
daemon is launched with — an interactive shell has your full PATH, but a
supervised daemon (launchd) gets a minimal one, so a bare ``opencode`` would not
be found. Resolving to an absolute path sidesteps that entirely.
"""
import os
import shutil
from pathlib import Path

# CLI runners Sisyphus knows how to drive: binary name -> runner type.
KNOWN = {
    "opencode": "opencode",
    "claude": "claude-cli",
}

# Common install dirs beyond PATH: curl installers, npm/pnpm/bun/homebrew globals.
_EXTRA_DIRS = [
    "~/.opencode/bin",
    "~/.superset/bin",
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/n/bin",
    "~/.volta/bin",
]


def resolve(binary: str):
    """Absolute path of a runner binary, or None. Searches: an already-absolute
    path, then PATH, then the common install dirs above."""
    if not binary:
        return None
    if os.path.isabs(binary):
        return binary if os.path.exists(binary) else None
    found = shutil.which(binary)
    if found:
        return found
    for d in _EXTRA_DIRS:
        p = Path(os.path.expanduser(d)) / binary
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def path_with_install_dirs(base: str = "") -> str:
    """*base* PATH with the CLI install dirs above appended (existing ones only).

    Resolving a runner to an absolute path is enough for US to spawn it, but
    says nothing about what THAT binary can find. Confirmed live: `claude` on
    this machine resolves to ~/.superset/bin/claude, which is a shell wrapper
    that searches PATH for the real binary (skipping its own directory) and
    exits with "claude not found in PATH" under the daemon's PATH of
    /usr/bin:/bin:/usr/sbin:/sbin. The worker died in 71ms and reported only
    "ended without reporting a result: no output". Anything a runner shells out
    to — wrappers, node, git — has the same exposure, so the worker gets the
    same search path a human shell would.

    Appended, never prepended: an explicit PATH from config still wins.
    """
    parts = [p for p in (base or "").split(os.pathsep) if p]
    seen = {os.path.normpath(p) for p in parts}
    for d in _EXTRA_DIRS:
        p = os.path.expanduser(d)
        if os.path.isdir(p) and os.path.normpath(p) not in seen:
            seen.add(os.path.normpath(p))
            parts.append(p)
    return os.pathsep.join(parts)


def detect() -> list:
    """Every known runner found on this machine: {name, type, binary, path}."""
    out = []
    for binary, rtype in KNOWN.items():
        path = resolve(binary)
        if path:
            out.append({"name": binary, "type": rtype, "binary": binary, "path": path})
    return out
