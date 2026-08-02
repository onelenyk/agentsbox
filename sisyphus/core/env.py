"""Minimal .env loader — zero dependency.

Loads ``KEY=VALUE`` lines from the app's ``.env`` into ``os.environ`` so secrets
(``OPENROUTER_API_KEY``, ``SISYPHUS_API_TOKEN``, …) are configured in one file
instead of the shell — which matters once the daemon is supervised (launchd),
where no interactive shell env exists.

Precedence: an already-set environment variable always wins (shell / launchd >
.env), matching python-dotenv's non-override default. Call once at process start
(daemon and CLI), before importing config or opening the DB.
"""
import os
from pathlib import Path

# The app root (…/sisyphus), where .env lives next to config.yaml.
_ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None) -> int:
    """Populate os.environ from a .env file. Returns how many keys were set.
    Existing env vars are never overwritten. Missing file is a no-op."""
    p = Path(path) if path else _ROOT / ".env"
    if not p.exists():
        return 0
    loaded = 0
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2) and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded
