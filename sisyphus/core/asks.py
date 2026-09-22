"""What a task in "Wants you" is asking of the person, and what can be done
about it from the card.

Four kinds, because they want four different things from you:

  question  someone asked something; answer it — often by picking an option
            the question itself numbered.
  stuck     the system broke (a runner returned nothing, workers keep dying,
            an internal error). There is nothing to decide, only to retry once
            the cause is fixed. These used to arrive dressed as questions —
            half of all the "questions" ever asked — and waited a day for an
            answer nobody knew to give.
  budget    a spending cap parked it; continue or leave it.
  merge     the work is done and its branch waits for you: ready, or blocked
            by the gate (a failed check, requested changes, no review).

Pure functions over rows the caller already has; nothing here reads the DB.
"""
import re
from typing import Optional

# The orchestrator's own words when it parks a task it cannot move
# (core/orchestrator.py). Matched on the start of the question so a rewording
# there shows up here as a failing test, not as a stuck task posing as a question.
_STUCK = (
    ("I couldn't produce a next-step decision", "The orchestrator returned nothing"),
    ("Something went wrong on my side", "An internal error stopped a pass"),
    ("The last few worker runs failed without reporting a result", "Workers keep ending without a result"),
)
_BUDGET_TAIL = "Reply to continue, or leave it parked."

_OPTION = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")
_ATTEMPTS = re.compile(r"(\d+) attempts?")
_ERROR = re.compile(r"\(status=(\w+)\):\s*(.+)$")
_PASSED = re.compile(r"(\d+) passed")
_FAILED_TEST = re.compile(r"^FAILED (\S+)", re.M)


def options(text: str) -> tuple:
    """(prompt, [(number, label)]) — a question's own numbered choices, if it
    offers two or more; the prompt is the text before the first of them."""
    lines = (text or "").strip().splitlines()
    opts, first = [], None
    for i, line in enumerate(lines):
        m = _OPTION.match(line)
        if m:
            if first is None:
                first = i
            label = m.group(2).strip()
            opts.append((m.group(1), (label[:90] + "…") if len(label) > 90 else label))
    if len(opts) < 2 or first is None:
        return (text or "").strip(), []
    prompt = "\n".join(lines[:first]).strip()
    prompt = re.sub(r"\s*(please )?pick one:?\s*$", "", prompt, flags=re.I).strip()
    return prompt, opts[:6]


def _last(thread: list, kind: str) -> Optional[dict]:
    return next((m for m in reversed(thread) if m["kind"] == kind), None)


def _last_error(thread: list) -> str:
    for m in reversed(thread):
        if m["kind"] == "worker_finished" and "ended without" in (m["text"] or ""):
            e = _ERROR.search(m["text"])
            return f"{e.group(1)} — {e.group(2)[:80]}" if e else m["text"][:90]
    return ""


def for_waiting(task: dict, thread: list) -> dict:
    """A waiting task as a card: its kind and what the card shows."""
    q = _last(thread, "question")
    text = (q["text"] if q else "").strip()
    asker = q["author"] if q else ""
    for prefix, headline in _STUCK:
        if text.startswith(prefix):
            n = _ATTEMPTS.search(text)
            return {"kind": "stuck", "headline": headline,
                    "attempts": int(n.group(1)) if n else None,
                    "error": _last_error(thread), "text": text, "asker": asker}
    if text.endswith(_BUDGET_TAIL):
        return {"kind": "budget", "headline": text[: -len(_BUDGET_TAIL)].strip() or "A spending cap was reached",
                "text": text, "asker": asker}
    prompt, opts = options(text)
    return {"kind": "question", "prompt": prompt or "Waiting for your reply.",
            "options": opts, "text": text, "asker": asker}


def diff_stats(patch: str) -> dict:
    files = adds = dels = 0
    for line in (patch or "").splitlines():
        if line.startswith("diff --git"):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return {"files": files, "adds": adds, "dels": dels}


def for_merge(mr: dict, gate: str, patch: str = "") -> dict:
    """A done task whose branch waits: the facts a merge rests on, and whether
    the card may offer a plain Merge (only when the gate says ready)."""
    review = (mr.get("review_status") or "").strip()
    check = (mr.get("check_status") or "").strip()
    out = (mr.get("check_output") or "")
    passed = _PASSED.search(out)
    broke = _FAILED_TEST.search(out)
    facts = []
    facts.append(("good", "review approved") if review == "approved" else
                 ("bad", "changes requested") if review == "changes_requested" else
                 ("bad", "the review died without a verdict") if review == "died" else
                 ("muted", "review in progress") if review == "pending" else
                 ("warn", "nobody has reviewed it"))
    if check == "passed":
        facts.append(("good", f"checks: {passed.group(1)} passed" if passed else "checks passed"))
    elif check == "failed":
        facts.append(("bad", "checks failed"))
    elif check == "not_run":
        facts.append(("muted", "no check configured"))
    else:
        facts.append(("muted", "no check recorded yet"))
    return {"kind": "merge", "gate": gate, "ready": gate == "ready", "facts": facts,
            "broke": broke.group(1) if broke else "", "diff": diff_stats(patch),
            "branch": f"{mr.get('source_branch', '')} → {mr.get('target_branch', '')}",
            "mr_id": mr["id"],
            "fix": {"check-failed": "The check failed on this branch — fix it so it passes, then hand it back for review.",
                    "check-stale": "The branch moved after its last check — run the check again.",
                    "changes": "The reviewer asked for changes — address them.",
                    "died": "The review died without a verdict — review this branch again.",
                    "unreviewed": "Nobody reviewed this branch — review it."}.get(gate, "")}
