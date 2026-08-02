"""Quick one-shot AI helpers via the local runner (opencode) — no new model HTTP.

Powers the New Task "AI structure" button: turn rough words (plus the picked
template) into a structured {goal, repo, agent}. Runs `opencode run <prompt>` in
a scratch dir and parses the JSON the model prints to stdout.
"""
import json
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_HINT = {
    "bug": "This is a bug fix: diagnose, fix, add a regression test, commit and open a merge request.",
    "feature": "This is a new feature: state clear acceptance criteria, add tests, commit and open a merge request.",
    "refactor": "This is a refactor: behaviour stays identical, update tests, commit and open a merge request.",
    "research": "This is research/planning: repo-less, ask for pros/cons and a recommendation, no code changes.",
    "review": "This is a review: verify correctness/style/tests, approve or request changes.",
}


def _prompt(text, template, repo_names, agent_names):
    hint = _HINT.get(template, "")
    return (
        "You convert a rough software task description into a structured task for "
        "an autonomous coding-agent system.\n"
        + (hint + "\n" if hint else "")
        + "\nAvailable repositories (use the EXACT name only if the text clearly refers "
        "to one, else leave empty): " + (", ".join(repo_names) or "(none)")
        + "\nAvailable agents (usually leave empty so the orchestrator dispatches): "
        + (", ".join(agent_names) or "(none)")
        + "\n\nRough description:\n\"\"\"\n" + text + "\n\"\"\"\n\n"
        "Return ONLY a single JSON object, no prose and no code fences, of the form:\n"
        '{"goal": "<clear, actionable, self-contained goal>", "repo": "<exact repo name or empty>", "agent": "<exact agent name or empty>"}'
    )


def run_prompt(prompt: str, timeout: int = 60) -> str:
    """One-shot local AI call through the default runner (opencode), forced to the
    runner's cheap/fast model. Returns the model's stdout text. No new model HTTP."""
    from core.runner_detect import resolve
    from core.runners import get_default_runner

    runner = get_default_runner()
    if not runner:
        raise RuntimeError("no default runner configured")
    binary = resolve(runner["binary"]) or runner["binary"]

    env = dict(os.environ)
    api_env = runner.get("api_key_env", "")
    if api_env and os.environ.get(api_env):
        env["OPENROUTER_API_KEY"] = os.environ[api_env]
    if runner.get("base_url"):
        env["OPENROUTER_BASE_URL"] = runner["base_url"]

    # Force the runner's model — opencode's default agent otherwise picks a slower
    # model and the call takes ~5x longer.
    model_args = ["--model", runner["model"]] if runner.get("model") else []
    with tempfile.TemporaryDirectory(prefix="sisyphus-ai-") as d:
        proc = subprocess.run([binary, "run", *model_args, prompt], capture_output=True,
                              text=True, timeout=timeout, cwd=d, env=env)
    return proc.stdout or ""


def parse_task(text, template="", repos=None, agents=None, timeout=60) -> dict:
    repos, agents = repos or [], agents or []
    repo_names = [r["name"] for r in repos]
    agent_names = [a["name"] for a in agents]
    out = run_prompt(_prompt(text, template, repo_names, agent_names), timeout=timeout)
    return _parse_out(out, repo_names, agent_names)


def _find_json(out: str):
    s = re.sub(r"```(?:json)?", "", out).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for m in reversed(re.findall(r"\{.*?\}", out, re.DOTALL)):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None


def _parse_out(out: str, repo_names, agent_names) -> dict:
    obj = _find_json(out)
    if not isinstance(obj, dict) or "goal" not in obj:
        return {"goal": "", "repo": "", "agent": ""}
    repo = str(obj.get("repo", "") or "").strip()
    agent = str(obj.get("agent", "") or "").strip()
    return {
        "goal": str(obj.get("goal", "") or "").strip(),
        "repo": repo if repo in repo_names else "",
        "agent": agent if agent in agent_names else "",
    }
