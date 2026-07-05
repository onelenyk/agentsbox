"""Pipeline YAML validator — catches errors at parse time before any step executes."""
from pathlib import Path
import yaml
from agents import load_agent, list_agents


def validate_pipeline(path: str) -> list[str]:
    """Validate a pipeline YAML file. Returns list of errors (empty = valid)."""
    content = Path(path).read_text()
    return validate_pipeline_string(content, str(Path(path).name))


def validate_pipeline_string(yaml_string: str, source: str = "inline") -> list[str]:
    """Validate pipeline YAML string. Returns list of errors."""
    errors = []
    try:
        data = yaml.safe_load(yaml_string)
    except yaml.YAMLError as e:
        return [f"{source}: YAML parse error: {e}"]

    if not isinstance(data, dict):
        return [f"{source}: root must be a mapping"]

    if "name" not in data:
        errors.append(f"{source}: missing required field 'name'")
    if "steps" not in data:
        errors.append(f"{source}: missing required field 'steps'")
        return errors

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        errors.append(f"{source}: 'steps' must be a list")
        return errors
    if len(steps) == 0:
        errors.append(f"{source}: 'steps' list is empty")
        return errors

    step_ids = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"{source}: step[{i}] must be a mapping")
            continue
        sid = step.get("id")
        if not sid:
            errors.append(f"{source}: step[{i}] missing 'id'")
            continue
        if sid in step_ids:
            errors.append(f"{source}: duplicate step id '{sid}'")
        step_ids.append(sid)

        agent_name = step.get("agent")
        if agent_name:
            try:
                load_agent(agent_name)
            except ValueError:
                avail = list_agents()
                errors.append(
                    f"{source}: step '{sid}' references unknown agent "
                    f"'{agent_name}'. Available: {avail}"
                )
        else:
            errors.append(f"{source}: step '{sid}' missing 'agent'")

    for step in steps:
        sid = step.get("id", "?")
        deps = step.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            if dep not in step_ids:
                errors.append(
                    f"{source}: step '{sid}' depends_on '{dep}' "
                    f"which is not a valid step id"
                )

    if step_ids:
        errors.extend(_detect_cycles(steps, step_ids, source))

    return errors


def _detect_cycles(steps: list, step_ids: list, source: str) -> list[str]:
    """DFS-based cycle detection. Returns list of error strings."""
    from collections import defaultdict

    graph = defaultdict(set)
    for step in steps:
        sid = step.get("id")
        if sid is None:
            continue
        deps = step.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            if dep in step_ids:
                graph[sid].add(dep)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in step_ids}
    errors = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for neighbor in graph[node]:
            if color[neighbor] == GRAY:
                cycle = path + [neighbor]
                errors.append(
                    f"{source}: circular dependency: {' -> '.join(cycle)}"
                )
            elif color[neighbor] == WHITE:
                dfs(neighbor, path + [neighbor])
        color[node] = BLACK

    for sid in step_ids:
        if color[sid] == WHITE:
            dfs(sid, [sid])

    return errors
