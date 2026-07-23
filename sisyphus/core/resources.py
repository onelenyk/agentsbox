import json
import os
from pathlib import Path
from typing import Optional, List

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
RUNS_DIR = Path(WORKSPACE_ROOT) / "runs"


def init_run_dir(run_id: str) -> str:
    """Create directory structure for a run."""
    run_dir = RUNS_DIR / run_id
    (run_dir / "resources").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return str(run_dir)


def save_resource(
    run_id: str,
    step_id: str,
    filename: str,
    content: str,
    step_order: int = 0,
    resource_type: str = "agent_output",
) -> str:
    """Write a resource file and register it in SQLite."""
    step_dir = RUNS_DIR / run_id / "resources" / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    filepath = step_dir / filename
    filepath.write_text(content, encoding="utf-8")

    from .state import register_resource

    register_resource(
        run_id, step_id, filename, rtype=resource_type, step_order=step_order
    )
    return str(filepath)


def list_resources(run_id: str) -> List[dict]:
    """Return all resources for a run from SQLite index."""
    from .state import get_resources_for_run

    return get_resources_for_run(run_id)


def read_resource(
    run_id: str, filename: str, step_id: Optional[str] = None
) -> Optional[str]:
    """Read a resource file. If step_id given, look in that step's subdirectory."""
    if step_id:
        filepath = RUNS_DIR / run_id / "resources" / step_id / filename
    else:
        filepath = RUNS_DIR / run_id / "resources" / filename
    return filepath.read_text(encoding="utf-8") if filepath.exists() else None


def get_available_resources(
    run_id: str, step_order: int, exclude_step_id: Optional[str] = None
) -> List[dict]:
    """Get resources from steps with order < current step.

    Excludes resources from the same step_id to enforce parallel sibling
    isolation — steps with the same step_order cannot read each other's output.
    """
    from .state import get_resources_for_run

    return get_resources_for_run(
        run_id,
        max_step_order=step_order - 1,
        exclude_step_id=exclude_step_id,
    )


_RESOURCE_TYPE_BY_FILENAME = {
    "result.json": "result",
    "analysis.md": "analysis",
}


def register_output_files(
    run_id: str,
    step_id: str,
    output_dir: str,
    step_order: int = 0,
    exclude: tuple = ("events.jsonl",),
) -> List[dict]:
    """Register every file the agent left in its output dir as a resource.

    Top-level regular files only; hidden files, *exclude* entries, and
    filenames already registered for this (run_id, step_id) are skipped —
    the latter dedupes setup-tool outputs written into the same dir before
    the agent ran.
    """
    from .state import get_resource_filenames_for_step, register_resource

    dir_path = Path(output_dir)
    if not dir_path.is_dir():
        return []

    already = get_resource_filenames_for_step(run_id, step_id)
    registered = []
    for entry in sorted(dir_path.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith(".") or name in exclude or name in already:
            continue
        rtype = _RESOURCE_TYPE_BY_FILENAME.get(name, "agent_output")
        registered.append(
            register_resource(run_id, step_id, name, rtype=rtype, step_order=step_order)
        )
    return registered


def save_agent_log(run_id: str, step_id: str, content: str) -> str:
    """Save agent stdout/stderr to logs directory."""
    log_dir = RUNS_DIR / run_id / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{step_id}.log"
    log_path.write_text(content, encoding="utf-8")
    return str(log_path)


def save_result_json(run_id: str, step_id: str, result: dict) -> str:
    """Convenience: save result.json in the step's resources directory."""
    return save_resource(
        run_id, step_id, "result.json", json.dumps(result, indent=2),
        resource_type="result",
    )


def save_analysis_md(run_id: str, step_id: str, analysis: str) -> str:
    """Convenience: save analysis.md in the step's resources directory."""
    return save_resource(
        run_id, step_id, "analysis.md", analysis, resource_type="analysis",
    )
