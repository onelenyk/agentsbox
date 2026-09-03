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


_ONESHOT_PREAMBLE = (
    "Answer IMMEDIATELY using ONLY the information in this prompt. Do NOT use any "
    "tools, do NOT read files, grep, or explore a codebase, do NOT call subagents — "
    "just produce the requested output directly and nothing else.\n\n"
)


def run_prompt(prompt: str, timeout: int = 60) -> str:
    """One-shot local AI call through the default runner (opencode), forced to the
    runner's cheap/fast model. Returns the model's stdout text. No new model HTTP.

    `opencode run` is an *agentic* runtime, not a plain completion: left to the
    user's global config it loads a heavy persona (plugins, skills, memory) and,
    prompted to "plan against a project", will wander off reading files in a
    tool-using loop — turning a ~10s answer into a 2-minute exploration that then
    times out or emits non-parseable tool noise. `--pure` drops the global
    plugins (we don't use project-local MCP here, so nothing we need is lost) and
    the preamble forbids tool use, making the call a fast, deterministic
    completion."""
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
        proc = subprocess.run([binary, "run", "--pure", *model_args, _ONESHOT_PREAMBLE + prompt],
                              capture_output=True, text=True, timeout=timeout, cwd=d, env=env)
    return proc.stdout or ""


def parse_task(text, template="", repos=None, agents=None, timeout=60) -> dict:
    repos, agents = repos or [], agents or []
    repo_names = [r["name"] for r in repos]
    agent_names = [a["name"] for a in agents]
    out = run_prompt(_prompt(text, template, repo_names, agent_names), timeout=timeout)
    return _parse_out(out, repo_names, agent_names)


def _brace_spans(text: str):
    """Every balanced ``{...}`` span in *text*, as (start, end).

    Braces inside JSON strings are skipped, so a value like ``"use {x} here"``
    cannot close its own object early.
    """
    spans, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            spans.append((stack.pop(), i + 1))
    return spans


def find_json(out: str):
    r"""The JSON object in a model's reply, or None.

    Models wrap their answer in prose, in fences, or both, so this cannot be a
    bare ``json.loads``. It also cannot be a regex, which is what it was — in
    three copied-and-since-diverged versions:

      ``\{.*\}``   spans from the first brace of the reply to the last, so one
                   aside ("here is the shape {approach, steps} you asked for")
                   swallowed the answer and parsed as nothing;
      ``\{.*?\}``  stops at the first ``}``, which truncates every nested
                   object — and a plan brief's own steps are objects, so that
                   was every brief.

    Both failed silently and returned None, which the caller reports to the
    user as "the model didn't return a usable plan": a retry loop over a reply
    that was fine.

    Braces are counted instead, and the candidates tried longest-first, then
    latest-first — an aside is shorter than the answer, and a model that shows
    a draft and then corrects itself puts the correction last.
    """
    s = re.sub(r"```(?:json)?", "", out).strip()
    try:
        return json.loads(s)
    except Exception as e:
        logger.debug("find_json: the whole reply is not JSON: %s", e)
    for start, end in sorted(_brace_spans(s),
                             key=lambda sp: (sp[1] - sp[0], sp[0]), reverse=True):
        try:
            return json.loads(s[start:end])
        except Exception as e:
            logger.debug("find_json: candidate %d..%d did not parse: %s", start, end, e)
    return None


# The name this module's own call sites (and its tests) already use.
_find_json = find_json


def classify_command(text: str, repos: list) -> dict:
    repo_names = [str(r) for r in repos] if repos else []
    prompt = (
        "You classify a user's plain-text command into one of three kinds. "
        "Return ONLY a single JSON object, no prose and no code fences.\n\n"
        "Kinds:\n"
        "- task: a small, well-scoped coding job (fix a bug, add a small feature, write tests)\n"
        "- mission: a multi-step, large-scope effort (redesign, migration, multiple deliverables, \"and then\", enumerated steps)\n"
        "- automation: a recurring/reactive intent (\"щодня\", \"every day\", \"щоразу коли\", \"when X happens\", cron, webhook, scheduled)\n\n"
        "Available repositories (use the EXACT name only if the text clearly refers "
        "to one, else leave empty): " + (", ".join(repo_names) or "(none)") + "\n\n"
        "User's command:\n\"\"\"\n" + text + "\n\"\"\"\n\n"
        "Return ONLY this JSON structure:\n"
        '{"kind": "task"|"mission"|"automation", "title": "short human title <=80 chars", "goal": "cleaned-up goal text", "repo": "exact repo name or empty", "cron": "cron string or empty (only for automation)"}'
    )
    try:
        out = run_prompt(prompt, timeout=60)
        obj = _find_json(out)
        if isinstance(obj, dict) and "kind" in obj:
            return {
                "kind": obj.get("kind", "task"),
                "title": str(obj.get("title", "") or "")[:80],
                "goal": str(obj.get("goal", "") or "") or text,
                "repo": str(obj.get("repo", "") or "").strip(),
                "cron": str(obj.get("cron", "") or "").strip() if obj.get("kind") == "automation" else "",
            }
    except Exception:
        logger.debug("classify_command: AI call failed, using fallback", exc_info=True)
    return _classify_fallback(text)


_AUTOMATION_KWS = ["every", "щодня", "щоночі", "webhook", "cron",
                    "every day", "every week", "every month", "every hour",
                    "each time", "щоразу", "whenever", "on schedule"]


def _classify_fallback(text: str) -> dict:
    t = text.strip()
    t_lower = t.lower()

    is_automation = any(kw in t_lower for kw in _AUTOMATION_KWS)
    if is_automation:
        kind = "automation"
    elif len(t) > 200 or _has_enumerated_steps(t):
        kind = "mission"
    else:
        kind = "task"

    first_line = t.split("\n")[0].strip()
    title = first_line[:80] if first_line else t[:80]

    return {"kind": kind, "title": title, "goal": t, "repo": "", "cron": "" if kind != "automation" else ""}


def _has_enumerated_steps(text: str) -> bool:
    import re
    for line in text.split("\n"):
        line = line.strip()
        if re.match(r"^\d+\.\s", line):
            return True
        if re.match(r"^[-*]\s", line):
            return True
    return False


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
