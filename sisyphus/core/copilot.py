"""Board Copilot — a chat that sees the whole task board and can propose actions.

Each user message is one one-shot local AI run (via ai_assist.run_prompt): we
inject a compact board snapshot + the chat history, and the model returns JSON —
either a plain reply, or a proposed action the user confirms before it runs.
Read is free; every mutating action is confirmed and then executed here.
"""
import json
import re

from core import ai_assist

_READ_KINDS = ("result", "question", "answer", "external")


def _last_meaningful(task_id: str) -> str:
    from core import store
    for m in reversed(store.get_thread(task_id)):
        if m["kind"] in _READ_KINDS or (m["author"] == "orchestrator" and m["kind"] == "note"):
            return f"{m['author']}/{m['kind']}: {m['text'][:140]}"
    return ""


def board_snapshot() -> str:
    from core import store, autopilot, usage
    lines = ["BOARD STATE"]
    tasks = [t for t in store.list_tasks() if not t.get("parent_id")]
    for status in ("active", "waiting", "done", "failed"):
        st = [t for t in tasks if t["status"] == status]
        if not st:
            continue
        lines.append(f"\n{status.upper()} ({len(st)}):")
        for t in st[:25]:
            last = _last_meaningful(t["id"])
            lines.append(f"  - {t['id']} [{t.get('repo') or 'no repo'}] {t['goal']}"
                         + (f"  · {last}" if last else ""))
    aps = autopilot.list_autopilots()
    if aps:
        lines.append("\nAUTOPILOTS:")
        for a in aps:
            trig = (a["cron"] or (f"every {a['interval_seconds']}s" if a["interval_seconds"]
                                  else f"webhook:{a['webhook_platform']}"))
            lines.append(f"  - {a['id']} '{a['name']}' [{'active' if a['enabled'] else 'paused'}] trigger={trig}")
    lines.append(f"\nSpend today: ${usage.today_cost():.4f}")
    return "\n".join(lines)


def _prompt(message: str, history: list, board: str, repo_names, agent_names) -> str:
    convo = "\n".join(f"{'User' if h.get('role') == 'user' else 'Copilot'}: {h.get('text', '')}"
                      for h in (history or [])[-8:])
    return (
        "You are the Board Copilot for Sisyphus, a system that runs autonomous "
        "coding agents. Help the user understand and manage their task board. Be "
        "concise.\n\n" + board +
        "\n\nAvailable repos: " + (", ".join(repo_names) or "(none)") +
        "\nAvailable worker agents: " + (", ".join(agent_names) or "(none)") +
        ("\n\nConversation so far:\n" + convo if convo else "") +
        "\n\nUser: " + message +
        "\n\nReturn ONLY one JSON object, no prose, no code fences:\n"
        '- to answer: {"reply": "<your answer>"}\n'
        '- to propose ONE action (the user will confirm before it runs): '
        '{"reply": "<one line: what you will do>", "action": {"type": "...", ...}}\n'
        "Action types (use EXACT ids from the board state; never invent ids):\n"
        '  {"type":"create_task","goal":"<clear, actionable goal>","repo":"<exact repo or empty>","agent":"<exact worker agent or empty>"}\n'
        '  {"type":"reply_task","task_id":"<id>","text":"<message to that task>"}\n'
        '  {"type":"pause_autopilot","id":"<autopilot id>"}\n'
        '  {"type":"resume_autopilot","id":"<autopilot id>"}\n'
    )


def chat(message: str, history: list) -> dict:
    """One copilot turn. Returns {reply, action?}. action (if present) is validated
    and awaits the user's confirmation — it is NOT executed here."""
    from core.repos import list_repos
    from core.agents import list_agents
    repo_names = [r["name"] for r in list_repos()]
    agent_names = [a["name"] for a in list_agents(kind="worker")]

    out = ai_assist.run_prompt(_prompt(message, history, board_snapshot(), repo_names, agent_names))
    obj = _find_json(out)
    if not isinstance(obj, dict):
        return {"reply": (out.strip()[:800] or "I couldn't produce a response — try rephrasing.")}
    reply = str(obj.get("reply", "") or "").strip()
    action = obj.get("action")
    if isinstance(action, dict) and action.get("type"):
        ok, err = validate_action(action, repo_names, agent_names)
        if not ok:
            return {"reply": (reply + f"\n\n(couldn't prepare that action: {err})").strip()}
        return {"reply": reply or "Proposed an action.", "action": action}
    return {"reply": reply or "…"}


def validate_action(action: dict, repo_names=None, agent_names=None):
    from core import store, autopilot
    from core.agents import list_agents
    if repo_names is None:
        from core.repos import list_repos
        repo_names = [r["name"] for r in list_repos()]
    if agent_names is None:
        agent_names = [a["name"] for a in list_agents(kind="worker")]
    t = action.get("type")
    if t == "create_task":
        if not str(action.get("goal", "")).strip():
            return False, "goal is required"
        if action.get("repo") and action["repo"] not in repo_names:
            return False, f"unknown repo {action['repo']!r}"
        if action.get("agent") and action["agent"] not in agent_names:
            return False, f"unknown agent {action['agent']!r}"
        return True, None
    if t == "reply_task":
        if not store.get_task(action.get("task_id", "")):
            return False, "unknown task"
        if not str(action.get("text", "")).strip():
            return False, "empty reply"
        return True, None
    if t in ("pause_autopilot", "resume_autopilot"):
        if not autopilot.get_autopilot(action.get("id", "")):
            return False, "unknown autopilot"
        return True, None
    return False, f"unknown action {t!r}"


def execute_action(action: dict) -> str:
    """Run a validated, user-confirmed action. Returns a short result line."""
    ok, err = validate_action(action)
    if not ok:
        raise ValueError(err)
    from core import store, autopilot
    from core.orchestrator import emit
    t = action["type"]
    if t == "create_task":
        task = store.create_task(str(action["goal"]).strip(), action.get("repo") or "",
                                 agent=action.get("agent") or None)
        emit(task["id"], "created")
        return f"Created task {task['id']}."
    if t == "reply_task":
        store.post_message(action["task_id"], "human", kind="answer", text=str(action["text"]).strip())
        emit(action["task_id"], "human_replied")
        return f"Replied to {action['task_id']}."
    if t == "pause_autopilot":
        autopilot.set_enabled(action["id"], False)
        return f"Paused autopilot {action['id']}."
    if t == "resume_autopilot":
        autopilot.set_enabled(action["id"], True)
        return f"Resumed autopilot {action['id']}."
    raise ValueError(f"unknown action {t!r}")


def _find_json(out: str):
    s = re.sub(r"```(?:json)?", "", out).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for m in reversed(re.findall(r"\{.*\}", out, re.DOTALL)):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None
