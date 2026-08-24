"""The written-down backlog: `docs/known-gaps.md` in a repo the system works on.

A gap gets into that file because someone used the system and found something
wrong. That makes it the one backlog the project actually has — and until now it
was readable only by a human opening the file, or by `tools/goal_report.py`
counting it. Anything that wants to answer "what is worth doing next" has to be
able to see it, so the parse lives here rather than in the tool.
"""
import os
import re
from typing import List

RELATIVE_PATH = os.path.join("docs", "known-gaps.md")

_ENTRY = re.compile(r"^### ", re.M)
_STATUS = re.compile(r"^\*\*Status:\*\*\s*(\S+)", re.M)


def open_gaps(path: str) -> dict:
    """{open, closed, titles}. An entry whose status does not say CLOSED counts
    as open — including one that forgot to say, which is the safe way to be
    wrong about our own ledger."""
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return {"open": 0, "closed": 0, "titles": []}

    titles: List[str] = []
    closed = 0
    for entry in _ENTRY.split(body)[1:]:
        status = _STATUS.search(entry)
        if status and status.group(1).upper().startswith("CLOSED"):
            closed += 1
        else:
            titles.append(entry.splitlines()[0].strip())
    return {"open": len(titles), "closed": closed, "titles": titles}


def ledger_path(repo: dict) -> str:
    """Where this repo keeps its ledger, or "" if it keeps none. Reads the
    checkout the repo was added from — not a worker's clone, which exists only
    while a run does.

    Looks at the repo root and then one directory down, because a repo root is
    not always the project root: this very project is added as `agentsbox`, and
    its ledger is at `sisyphus/docs/known-gaps.md`. One level, sorted, first
    match — deep enough for the monorepo case, shallow enough that it cannot
    wander into a vendored copy and read someone else's backlog.
    """
    source = (repo.get("source") or "").strip()
    if not source or "://" in source:
        return ""      # a remote URL has no file we can read from here

    root = os.path.join(source, RELATIVE_PATH)
    if os.path.isfile(root):
        return root
    try:
        children = sorted(d for d in os.listdir(source)
                          if not d.startswith(".") and os.path.isdir(os.path.join(source, d)))
    except OSError:
        return ""
    for child in children:
        path = os.path.join(source, child, RELATIVE_PATH)
        if os.path.isfile(path):
            return path
    return ""


def open_gaps_by_repo() -> dict:
    """{repo_name: {open, closed, titles}} for every configured repo that keeps
    a ledger. Repos without one are simply absent — no entry, no zero row, so a
    reader cannot mistake "does not keep one" for "has nothing open"."""
    from .repos import list_repos

    out = {}
    for repo in list_repos():
        path = ledger_path(repo)
        if path:
            out[repo["name"]] = open_gaps(path)
    return out
