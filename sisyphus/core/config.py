import os, re
from pathlib import Path
from string import Template
import yaml

_config = None

def config_file() -> Path:
    """config.yaml when it exists, else the tracked config.example.yaml — so a
    fresh checkout runs with documented defaults (and the ${VAR} placeholders
    the .env keys plug into) instead of a silently empty config. The daemon's
    first write (Repos → Add) materialises config.yaml from that base."""
    root = Path(__file__).parent.parent
    real = root / "config.yaml"
    return real if real.exists() else root / "config.example.yaml"


def load_config():
    global _config
    if _config is not None:
        return _config
    config_path = config_file()
    if not config_path.exists():
        _config = {}
        return _config
    raw = config_path.read_text()
    def _replace(m):
        return os.environ.get(m.group(1), m.group(0))
    resolved = re.sub(r'\$\{(\w+)\}', _replace, raw)
    _config = yaml.safe_load(resolved) or {}
    return _config
def reload_config():
    """Force reload config from disk."""
    global _config
    _config = None
    return load_config()


def get_config(key=None, default=None):
    config = load_config()
    if key is None:
        return config
    parts = key.split(".")
    value = config
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return default
    return value if value is not None else default


# Names the daemon sets for its own isolation. worker_env must never win over
# these: WORKSPACE_ROOT in particular is what stops a worker's tool calls (and
# its test runs) from resolving the live daemon database.
_RESERVED_WORKER_ENV = ("WORKSPACE_ROOT", "SISYPHUS_OUTPUT_DIR", "SISYPHUS_WORKTREE",
                        "SISYPHUS_DB_READONLY", "SISYPHUS_DB_PROTECT", "SISYPHUS_MODEL")


def worker_env(base=None):
    """Extra environment for worker subprocesses, from the `worker_env` config block.

    A worker inherits the daemon's environment, and the daemon is normally
    started by launchd — whose environment is nearly empty
    (PATH=/usr/bin:/bin:/usr/sbin:/sbin, no JAVA_HOME, no ANDROID_HOME). So any
    project needing a real toolchain fails on its first command: a live Android
    task reported "Unable to locate a Java Runtime" with Java 21 installed, and
    never executed a line of the code it wrote.

    Values are expanded against the daemon's environment plus the entries
    already resolved from this block, so later keys may build on earlier ones
    (PATH: "${JAVA_HOME}/bin:${PATH}"), and `~` is expanded.

    NOTE — there are TWO substitution passes, and knowing which one ran is the
    difference between a working PATH and a puzzling one:

    1. ``load_config`` rewrites every ``${VAR}`` in the raw YAML from the
       DAEMON's environment before parsing. So ``${PATH}`` is already the
       daemon's PATH here — which is what you want when appending to it.
    2. This function then substitutes what pass 1 left alone, against the keys
       resolved from this block. That is how ``${JAVA_HOME}`` works: launchd
       gives the daemon no JAVA_HOME, so pass 1 leaves it literal and pass 2
       fills it from the entry above.

    Consequence worth remembering: verifying this from an interactive shell is
    misleading, because pass 1 picks up your rich shell PATH rather than
    launchd's four directories. Verify with `env -i PATH=/usr/bin:/bin ...`.
    """
    raw = get_config("worker_env", {}) or {}
    if not isinstance(raw, dict):
        return {}
    env = dict(base if base is not None else os.environ)
    out = {}
    for name, value in raw.items():
        if name in _RESERVED_WORKER_ENV:
            continue          # the daemon owns these; silently ignoring is wrong,
                              # so the caller logs it — see launcher
        # Substitute against `env`, NOT os.environ: os.path.expandvars ignores
        # any mapping you give it, so "${PATH}" would silently resolve to the
        # daemon's PATH even when resolving against a different base. Unknown
        # names are left as literal text by safe_substitute, which surfaces a
        # typo instead of quietly emptying PATH.
        resolved = os.path.expanduser(Template(str(value)).safe_substitute(env))
        env[name] = resolved
        out[name] = resolved
    return out


def worker_env_rejected():
    """Names in `worker_env` that were ignored because the daemon owns them."""
    raw = get_config("worker_env", {}) or {}
    if not isinstance(raw, dict):
        return []
    return [n for n in raw if n in _RESERVED_WORKER_ENV]
