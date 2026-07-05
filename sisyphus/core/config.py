import os, re
from pathlib import Path
import yaml

_config = None

def load_config():
    global _config
    if _config is not None:
        return _config
    config_path = Path(__file__).parent.parent / "config.yaml"
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
