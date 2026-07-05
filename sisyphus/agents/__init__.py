from pathlib import Path
import yaml

_AGENTS_DIR = Path(__file__).parent


def load_agent(name: str) -> dict:
    """Load an agent definition by name (without .yaml extension)."""
    filepath = _AGENTS_DIR / f"{name}.yaml"
    if not filepath.exists():
        raise ValueError(f"Unknown agent: {name}. Available: {list_agents()}")
    return yaml.safe_load(filepath.read_text())


def list_agents() -> list:
    """Return list of available agent names."""
    return sorted([f.stem for f in _AGENTS_DIR.glob("*.yaml")])
