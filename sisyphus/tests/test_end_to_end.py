"""End-to-end: a task becomes a commit, a merge request, a verified check, a merge.

Every part of this path has its own unit tests and the path itself had none —
the assurance that it works came from a human launching a task and reading the
result, which does not survive the session it happened in.

The model is deliberately absent. What an agent *decides* cannot be asserted and
costs money to sample; what the machine does with those decisions is exactly
what breaks silently. So this drives the real MCP tool handlers in the order a
worker calls them, against a real git repo, and checks that the artifacts are
real: a commit on the task branch, an MR with a diff, a check result the daemon
produced, and a merge that lands on the target.

The complementary half — that a runner CLI can be spawned and reach these tools
at all — is core.runner_smoke.
"""
import os
import subprocess

import pytest

from core import git_ops, mergerequests, sessions, store, verify, worktree
from web import agent_mcp


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


@pytest.fixture
def project(tmp_path, monkeypatch, db_path):
    """A registered repo, a task, and a worktree — the state spawn_worker leaves
    behind just before it hands control to the agent."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(worktree, "REPOS_DIR", tmp_path / "repos")

    src = tmp_path / "myrepo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (src / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")

    repo = {"name": "myrepo", "source": str(src), "branch": "main",
            "check": "python3 -m pytest -q"}
    monkeypatch.setattr("core.repos.get_repo", lambda name: repo if name == "myrepo" else None)
    monkeypatch.setattr("core.repos.load_repos", lambda: [repo])

    mergerequests.init_mr_table()
    task = store.create_task("add a multiply function", "myrepo")

    bare = str(tmp_path / "repos" / f"{task['id']}_myrepo")
    worktree.init_bare_clone(str(src), bare)
    branch = git_ops.ensure_task_branch(bare, task["id"], "main")
    wt = worktree.create_worktree(bare, task["id"], "engineer", branch)

    session = {"task_id": task["id"], "role": "engineer", "worktree": wt}
    return {"task": task, "src": src, "bare": bare, "branch": branch,
            "worktree": wt, "session": session}


def _engineer_writes_code(wt):
    """What an agent's editing tools would have left behind."""
    with open(os.path.join(wt, "calc.py"), "a") as fh:
        fh.write("\n\ndef multiply(a, b):\n    return a * b\n")
    with open(os.path.join(wt, "test_calc.py"), "a") as fh:
        fh.write("\n\ndef test_multiply():\n    from calc import multiply\n"
                 "    assert multiply(2, 3) == 6\n")


class TestTaskToMerge:
    def test_the_whole_path(self, project, monkeypatch):
        s = project["session"]
        _engineer_writes_code(project["worktree"])

        # 1. commit
        res = agent_mcp.TOOLS["commit"][2](s, message="feat: add multiply")
        assert res.get("ok"), res
        sha = res["sha"]
        assert sha in _git(project["bare"], "log", "--format=%H", project["branch"]).stdout

        # The commit reached the thread, which is the only durable state.
        kinds = [m["kind"] for m in store.get_thread(s["task_id"])]
        assert "commit" in kinds

        # 2. open the merge request
        monkeypatch.setattr("core.reviews.ensure_review_task", lambda mr: None)
        res = agent_mcp.TOOLS["open_mr"][2](s, title="Add multiply", description="")
        assert "error" not in res, res

        mr = mergerequests.latest_for_task(s["task_id"])
        assert mr and mr["status"] == "open"
        assert mr["source_branch"] == project["branch"]
        assert mr["target_branch"] == "main"

        # 3. the daemon's own check, against the branch — not the agent's word
        result = verify.run_and_record(mr)
        assert result["ran"] is True and result["ok"] is True, result
        assert mergerequests.get_mr(mr["id"])["check_status"] == "passed"

        # 4. merge, and the change is really in the user's repo
        merged = mergerequests.merge_branch(mr, "main")
        assert merged["ok"], merged
        assert "def multiply" in _git(project["src"], "show", "main:calc.py").stdout

    def test_a_failing_branch_is_reported_as_failing(self, project):
        """The check is the gate. If it cannot fail, it is decoration."""
        s = project["session"]
        with open(os.path.join(project["worktree"], "test_calc.py"), "a") as fh:
            fh.write("\n\ndef test_broken():\n    assert 1 == 2\n")
        agent_mcp.TOOLS["commit"][2](s, message="feat: add a failing test")
        agent_mcp.TOOLS["open_mr"][2](s, title="Broken", description="")

        mr = mergerequests.latest_for_task(s["task_id"])
        result = verify.run_and_record(mr)
        assert result["ok"] is False
        assert mergerequests.get_mr(mr["id"])["check_status"] == "failed"
        # And the reviewer is confronted with it on the thread.
        assert any("FAILED" in (m["text"] or "") for m in store.get_thread(s["task_id"]))

    def test_the_baseline_handed_over_spans_the_whole_branch(self, project, monkeypatch):
        """With two commits on the branch, HEAD~1 would hide the first one —
        the mistake that once let a mode change through review."""
        s = project["session"]
        _engineer_writes_code(project["worktree"])
        agent_mcp.TOOLS["commit"][2](s, message="first")
        with open(os.path.join(project["worktree"], "calc.py"), "a") as fh:
            fh.write("\n\ndef divide(a, b):\n    return a / b\n")
        agent_mcp.TOOLS["commit"][2](s, message="second")

        monkeypatch.setattr("core.reviews.ensure_review_task", lambda mr: None)
        agent_mcp.TOOLS["open_mr"][2](s, title="Two commits", description="")
        mr = mergerequests.latest_for_task(s["task_id"])

        out = agent_mcp.TOOLS["get_mr_diff"][2](s, mr_id=mr["id"])
        prev = _git(project["bare"], "rev-parse", f"{project['branch']}~1").stdout.strip()
        assert out["base_sha"] and out["base_sha"] != prev
        # Both commits are inside the diff the reviewer is given.
        assert "def multiply" in out["diff"] and "def divide" in out["diff"]


class TestOrderIsEnforced:
    def test_no_merge_request_before_a_commit(self, project):
        res = agent_mcp.TOOLS["open_mr"][2](project["session"], title="Nope", description="")
        assert "error" in res and "commit" in res["error"]

    def test_an_empty_commit_is_refused(self, project):
        res = agent_mcp.TOOLS["commit"][2](project["session"], message="nothing changed")
        assert "error" in res and "nothing to commit" in res["error"]

    def test_a_commit_needs_a_message(self, project):
        _engineer_writes_code(project["worktree"])
        assert "error" in agent_mcp.TOOLS["commit"][2](project["session"], message="  ")


class TestScaffoldingStaysOutOfCommits:
    def test_runner_config_is_never_committed(self, project):
        """A worker's cwd is the worktree, so whatever its CLI writes there is
        one `git add -A` away from a user's repo."""
        s = project["session"]
        _engineer_writes_code(project["worktree"])
        for junk in ("opencode.json", ".claude", ".sisyphus"):
            p = os.path.join(project["worktree"], junk)
            if junk.startswith("."):
                os.makedirs(p, exist_ok=True)
                with open(os.path.join(p, "state.json"), "w") as fh:
                    fh.write("{}")
            else:
                with open(p, "w") as fh:
                    fh.write("{}")

        res = agent_mcp.TOOLS["commit"][2](s, message="feat: add multiply")
        assert res.get("ok"), res
        files = _git(project["bare"], "show", "--name-only", "--format=", res["sha"]).stdout
        assert "calc.py" in files
        for junk in ("opencode.json", ".claude", ".sisyphus"):
            assert junk not in files, f"{junk} was committed into the user's repo"
