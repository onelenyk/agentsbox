"""Quick one-shot AI helpers via the local runner (opencode) — no new model HTTP.

Used by the planner: `run_prompt` runs `opencode run
<prompt>` in a scratch dir, and `find_json` reads the JSON the model prints.
"""
import json
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)


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

    from core import runner_profiles
    env = dict(os.environ)
    env.update(runner_profiles.build_env("opencode", runner))

    # Force the runner's model — opencode's default agent otherwise picks a slower
    # model and the call takes ~5x longer.
    model_args = ["--model", runner["model"]] if runner.get("model") else []
    with tempfile.TemporaryDirectory(prefix="sisyphus-ai-") as d:
        proc = subprocess.run([binary, "run", "--pure", *model_args, _ONESHOT_PREAMBLE + prompt],
                              capture_output=True, text=True, timeout=timeout, cwd=d, env=env)
    return proc.stdout or ""


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
