"""Jinja2 prompt template rendering for Sisyphus agents."""
import os
from pathlib import Path
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateNotFound

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=[]),
)


def render_prompt(
    agent_def: dict, card: dict, step: dict, resources: Optional[list] = None
) -> str:
    """Render a complete agent prompt from agent definition, card, step, and resources.

    Args:
        agent_def: agent YAML dict (name, role, access, output_fields, etc.)
        card: card dict (description, platforms, etc.)
        step: step dict (id, worktree_path, etc.)
        resources: list of resource dicts from previous steps
    """
    agent_name = agent_def.get("name", "Unknown")

    # Determine template — reviewer gets special template, rest use base
    template_name = "reviewer.j2" if agent_name == "Code Reviewer" else "base.j2"

    try:
        template = _env.get_template(template_name)
    except TemplateNotFound:
        template = _env.get_template("base.j2")

    workspace_root = os.environ.get("WORKSPACE_ROOT", "/workspace")
    output_dir = (
        f"{workspace_root}/runs/{step.get('run_id', 'unknown')}"
        f"/resources/{step.get('id', 'unknown')}"
    )

    task_description = card.get(
        "description", "Analyze the task and produce your output."
    )
    if card.get("jira_url"):
        task_description += f"\nJira Issue: {card['jira_url']}"

    return template.render(
        agent=agent_def,
        card=card,
        step=step,
        resources=resources or [],
        task_description=task_description,
        output_dir=output_dir,
    )
