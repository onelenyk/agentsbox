"""Worker role presets — the entire "role" concept.

A role is nothing more than a name → (persona prompt + allowed tool subset).
The only thing a role *constrains* is which agent-MCP tools it may call; the
persona is just prompt text. Add a role by adding a dict entry.
"""

# Base tools every worker gets.
_BASE = ["get_context", "comment", "attach"]

ROLES = {
    "explorer": {
        "persona": (
            "You investigate the codebase — read files, search patterns, trace "
            "how things work. You do NOT modify code. Report findings, then finish. "
            "If the task spans multiple repositories (e.g. an iOS app and an "
            "Android app), call propose_subtasks with one {repo, goal} entry per "
            "repo so the work can be fanned out — then finish."
        ),
        "tools": _BASE + ["propose_subtasks", "finish"],
    },
    "engineer": {
        "persona": (
            "You implement the change in the working tree. Commit your work, "
            "open a merge request when ready, then finish. "
            "Git discipline: your worktree is the ONLY place you touch. Never "
            "run git (or edit files) against any path outside it — the real "
            "source repo is write-protected by a sandbox and any attempt fails "
            "with 'Operation not permitted'. Never run `git push`: publishing "
            "is not your job — the `open_mr` tool records the change without "
            "pushing anywhere. Commit ONLY via the `commit` tool, not raw git."
        ),
        "tools": _BASE + ["commit", "open_mr", "finish"],
    },
    "reviewer": {
        "persona": (
            "You review the work against the goal. End with either approve or "
            "request_changes (with a clear reason). That is how you finish."
        ),
        "tools": _BASE + ["approve", "request_changes"],
    },
}


def get_role(name: str) -> dict:
    if name not in ROLES:
        raise ValueError(f"unknown role {name!r}; known: {list(ROLES)}")
    return ROLES[name]


def role_tools(name: str) -> list:
    return list(get_role(name)["tools"])


def list_roles() -> list:
    return list(ROLES)
