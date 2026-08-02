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


def detect() -> list:
    """Every known runner found on this machine: {name, type, binary, path}."""
    out = []
    for binary, rtype in KNOWN.items():
        path = resolve(binary)
        if path:
            out.append({"name": binary, "type": rtype, "binary": binary, "path": path})
    return out
