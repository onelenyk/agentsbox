from pathlib import Path
import yaml

_AGENTS_DIR = Path(__file__).parent


def load_agent(name: str, validate: bool = True) -> dict:
    """Load an agent definition by name (without .yaml extension).

    Args:
        name: Agent name (filename stem, without .yaml).
        validate: If True (default), validate output_fields against
                  BaseResult.model_fields to catch typos and drift.
    """
    filepath = _AGENTS_DIR / f"{name}.yaml"
    if not filepath.exists():
        raise ValueError(f"Unknown agent: {name}. Available: {list_agents()}")
    agent_def = yaml.safe_load(filepath.read_text())

    if validate:
        _validate_output_fields(agent_def)

    return agent_def


def _validate_output_fields(agent_def: dict) -> None:
    """Validate that every output_fields key exists on BaseResult.model_fields.

    Raises ValueError for any unknown field — this catches typos and
    prevents drift between agent YAML declarations and the actual schema.
    """
    from core.schemas import BaseResult

    output_fields = agent_def.get("output_fields")
    if not output_fields:
        return  # agents without output_fields are valid

    valid_fields = set(BaseResult.model_fields.keys())
    for field in output_fields:
        if field not in valid_fields:
            raise ValueError(
                f"Agent '{agent_def.get('name', 'unknown')}' declares unknown "
                f"output_field '{field}'. Valid fields: {sorted(valid_fields)}"
            )


def list_agents() -> list:
    """Return list of available agent names."""
    return sorted([f.stem for f in _AGENTS_DIR.glob("*.yaml")])
