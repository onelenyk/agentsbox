"""Tests for web/agent_mcp.py — the agent-facing MCP tool surface.

Drives the HTTP MCP endpoint like a worker would: mint a session, then POST
JSON-RPC with the Bearer token. Verifies role-scoped tools, thread writes, and
preconditions.
"""
import json
import os
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
from web.app import app

from core import sessions, store


def _client():
    return TestClient(app)


def _rpc(client, token, method, params=None, req_id=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/agent", json=body, headers=headers)


def _call(client, token, name, args=None):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": args or {}}).json()


def _session(db_path, role="explorer"):
    sessions.clear()
    task = store.create_task("do the thing", repo="sisyphus")
    token = sessions.mint(task["id"], role)
    return task["id"], token


def _grant(role, *tools):
    from core import agents
    current = agents.agent_tools(role)
    new = list(set(current) | set(tools))
    agents.update_agent(role, tools=new)


# ── protocol ────────────────────────────────────────────────────────────


def test_initialize(db_path):
    task_id, token = _session(db_path)
    r = _rpc(_client(), token, "initialize").json()
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["serverInfo"]["name"] == "sisyphus-agent"


def test_notification_gets_202(db_path):
    _, token = _session(db_path)
    r = _rpc(_client(), token, "notifications/initialized")
    assert r.status_code == 202


def test_missing_token_rejected(db_path):
    _session(db_path)
    r = _rpc(_client(), None, "tools/list").json()
    assert "error" in r
    assert "session" in r["error"]["message"]


# ── role-scoped tools ───────────────────────────────────────────────────


def test_tools_list_matches_role(db_path):
    c = _client()
    for role, expected in [
        ("explorer", {"get_context", "comment", "attach", "propose_subtasks", "finish"}),
        ("engineer", {"get_context", "comment", "attach", "commit", "open_mr", "finish"}),
        ("reviewer", {"get_context", "comment", "attach", "approve", "request_changes"}),
    ]:
        _, token = _session(db_path, role)
        tools = _rpc(c, token, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == expected


def test_tool_outside_role_rejected(db_path):
    task_id, token = _session(db_path, "explorer")
    out = _call(_client(), token, "commit", {"message": "x"})
    assert out["result"]["isError"] is True
    assert "not permitted" in json.loads(out["result"]["content"][0]["text"])["error"]


# ── thread writes ───────────────────────────────────────────────────────


def test_comment_writes_thread(db_path):
    task_id, token = _session(db_path)
    _call(_client(), token, "comment", {"text": "found the bug"})
    thread = store.get_thread(task_id)
    assert thread[-1]["author"] == "explorer"
    assert thread[-1]["text"] == "found the bug"


def test_attach_persists_file_and_records_filename(db_path, tmp_path):
    sessions.clear()
    task = store.create_task("do the thing", repo="sisyphus")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "analysis.md").write_text("the findings")
    token = sessions.mint(task["id"], "explorer", worktree=str(wt))

    out = _call(_client(), token, "attach", {"filename": "analysis.md", "description": "the plan"})
    assert out["result"]["isError"] is False
    assert store.get_thread(task["id"])[-1]["attachment"] == "analysis.md"
    # persisted into the durable store so it survives worktree cleanup
    from core import attachments
    assert attachments.read(task["id"], "analysis.md") == "the findings"


def test_attach_missing_file_errors(db_path, tmp_path):
    sessions.clear()
    task = store.create_task("x", repo="sisyphus")
    wt = tmp_path / "wt"; wt.mkdir()
    token = sessions.mint(task["id"], "explorer", worktree=str(wt))
    out = _call(_client(), token, "attach", {"filename": "nope.md"})
    assert out["result"]["isError"] is True


def test_attach_rejects_path(db_path):
    task_id, token = _session(db_path)
    out = _call(_client(), token, "attach", {"filename": "../etc/passwd"})
    assert out["result"]["isError"] is True


def test_attachment_served_over_api(db_path):
    from core import attachments
    task = store.create_task("x", repo="sisyphus")
    attachments.save_text(task["id"], "change.patch", "diff --git a/x b/x\n")
    r = _client().get(f"/api/v1/tasks/{task['id']}/attachments/change.patch")
    assert r.status_code == 200
    assert "diff --git" in r.text
    assert _client().get(f"/api/v1/tasks/{task['id']}/attachments/missing.txt").status_code == 404


def test_finish_posts_result_and_revokes(db_path):
    task_id, token = _session(db_path)
    out = _call(_client(), token, "finish", {"summary": "all done"})
    assert out["result"]["isError"] is False
    kinds = [m["kind"] for m in store.get_thread(task_id)]
    assert "result" in kinds and "worker_finished" in kinds
    # session revoked → further calls fail
    assert "error" in _rpc(_client(), token, "tools/list").json()


def test_finish_requires_summary(db_path):
    _, token = _session(db_path)
    out = _call(_client(), token, "finish", {"summary": "  "})
    assert out["result"]["isError"] is True


def test_finish_refused_on_a_review_task(db_path):
    """A verdict only reaches the MR through approve/request_changes. Finishing
    with prose left mr-1dc636dd stranded at "pending" while its review task sat
    done — so the tool refuses instead of trusting the prompt.
    """
    sessions.clear()
    task = store.create_task(
        "Review-of-MR: mr-abc123\nOrigin-task: task-xyz\n\nReview it.",
        repo="sisyphus")
    # the guard keys off the task's goal, not the agent — any role holding
    # `finish` must be refused on a review task
    token = sessions.mint(task["id"], "explorer")
    out = _call(_client(), token, "finish", {"summary": "looks good to me"})
    assert out["result"]["isError"] is True
    assert "approve" in out["result"]["content"][0]["text"]
    # and the task is NOT quietly finished
    assert "result" not in [m["kind"] for m in store.get_thread(task["id"])]


def test_finish_still_works_for_a_multi_mr_sweep(db_path):
    """The daily autopilot reviews every open MR at once — no single verdict to
    write, so that task must still be able to finish with a report.
    """
    sessions.clear()
    task = store.create_task("Review all open merge-request branches and report",
                             repo="sisyphus")
    token = sessions.mint(task["id"], "explorer")
    out = _call(_client(), token, "finish", {"summary": "3 MRs scanned, 1 stale"})
    assert out["result"]["isError"] is False
    assert "result" in [m["kind"] for m in store.get_thread(task["id"])]


def test_finish_hook_fires(db_path):
    import web.agent_mcp as amcp
    fired = []
    amcp.on_worker_finished = lambda tid: fired.append(tid)
    try:
        task_id, token = _session(db_path)
        _call(_client(), token, "finish", {"summary": "done"})
        assert fired == [task_id]
    finally:
        amcp.on_worker_finished = None


# ── preconditions (the "when" enforced by the tool) ─────────────────────


def test_open_mr_requires_prior_commit(db_path):
    # commit / open_mr do real git + publishing; mock those layers so this test
    # covers the MCP wiring (precondition + thread writes), not git mechanics.
    task_id, token = _session(db_path, "engineer")
    c = _client()
    early = _call(c, token, "open_mr", {"title": "Fix", "description": "..."})
    assert early["result"]["isError"] is True  # no commit yet

    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="a" * 40):
        _call(c, token, "commit", {"message": "fix: retry"})
    with patch("core.providers.publish_change", return_value={
        "ok": True, "platform": "local", "branch": "sisyphus/x",
        "stat": " feature.py | 2 +-", "patch_file": "change.patch",
    }):
        ok = _call(c, token, "open_mr", {"title": "Fix", "description": "..."})
    assert ok["result"]["isError"] is False
    mr = [m for m in store.get_thread(task_id) if m["kind"] == "mr"]
    assert mr and mr[0]["attachment"] == "change.patch"
    assert "sisyphus/x" in mr[0]["text"]


def test_open_mr_twice_creates_one_row_refreshes_patch(db_path):
    task_id, token = _session(db_path, "engineer")
    c = _client()

    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="a" * 40):
        _call(c, token, "commit", {"message": "fix: one"})

    from core import mergerequests

    first_mr_id = None

    def _pub(session, title, desc):
        nonlocal first_mr_id
        from core import attachments
        attachments.save_text(session["task_id"], "change.patch",
                              f"diff for {title}\n")
        existing = mergerequests.find_open_mr(
            session["task_id"], "sisyphus/x", "main")
        if existing:
            mr = mergerequests.update_mr(existing["id"], title=title)
        else:
            mr = mergerequests.create_mr(
                task_id=session["task_id"], repo="test-repo",
                source_branch="sisyphus/x", target_branch="main",
                title=title, bare_path="/tmp/bare",
                base_branch="main", source_path="/tmp/src",
            )
        first_mr_id = mr["id"]
        return {"ok": True, "platform": "local", "branch": "sisyphus/x",
                "stat": " file.py | 1 +", "patch_file": "change.patch",
                "mr_id": mr["id"]}

    with patch("core.providers.publish_change", side_effect=_pub):
        _call(c, token, "open_mr", {"title": "Fix one"})

    with patch("core.providers.publish_change", side_effect=_pub):
        _call(c, token, "open_mr", {"title": "Fix two"})

    open_rows = [mr for mr in mergerequests.list_mrs(status="open")
                 if mr["task_id"] == task_id]
    assert len(open_rows) == 1
    assert open_rows[0]["title"] == "Fix two"
    assert open_rows[0]["id"] == first_mr_id

    from core import attachments
    patch_content = attachments.read(task_id, "change.patch")
    assert "diff for Fix two" in (patch_content or "")


def test_commit_reports_sha_and_records_it(db_path):
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="deadbeef" + "0" * 32):
        out = _call(_client(), token, "commit", {"message": "add thing"})
    assert out["result"]["isError"] is False
    assert json.loads(out["result"]["content"][0]["text"])["sha"].startswith("deadbeef")
    commits = [m for m in store.get_thread(task_id) if m["kind"] == "commit"]
    assert commits and "deadbeef" in commits[0]["text"]


def test_commit_with_no_changes_errors(db_path):
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value=None):
        out = _call(_client(), token, "commit", {"message": "nothing"})
    assert out["result"]["isError"] is True
    assert "nothing to commit" in json.loads(out["result"]["content"][0]["text"])["error"]
    assert not any(m["kind"] == "commit" for m in store.get_thread(task_id))


def test_commit_without_repo_errors_clearly(db_path):
    """A repo-less task's scratch dir isn't a git repo — commit tells the worker
    to report via finish() instead of failing with a raw git error."""
    task_id, token = _session(db_path, "engineer")
    with patch("core.git_ops.is_git_repo", return_value=False):
        out = _call(_client(), token, "commit", {"message": "add thing"})
    assert out["result"]["isError"] is True
    assert "no repo" in json.loads(out["result"]["content"][0]["text"])["error"]
    assert not any(m["kind"] == "commit" for m in store.get_thread(task_id))


def test_request_changes_requires_reason(db_path):
    _, token = _session(db_path, "reviewer")
    out = _call(_client(), token, "request_changes", {"reason": ""})
    assert out["result"]["isError"] is True


def test_reviewer_approve_finishes(db_path):
    task_id, token = _session(db_path, "reviewer")
    out = _call(_client(), token, "approve", {"summary": "looks good"})
    assert json.loads(out["result"]["content"][0]["text"])["verdict"] == "approved"
    assert "worker_finished" in [m["kind"] for m in store.get_thread(task_id)]


def test_approve_does_not_double_prefix(db_path):
    task_id, token = _session(db_path, "reviewer")
    _call(_client(), token, "approve", {"summary": "APPROVED -- all tests passed"})
    results = [m for m in store.get_thread(task_id) if m["kind"] == "result"]
    assert results
    text = results[0]["text"]
    assert text.startswith("APPROVED")
    assert "APPROVED: APPROVED" not in text
    assert text == "APPROVED -- all tests passed"


def test_request_changes_does_not_double_prefix(db_path):
    task_id, token = _session(db_path, "reviewer")
    _call(_client(), token, "request_changes", {"reason": "CHANGES REQUESTED -- needs more test coverage"})
    results = [m for m in store.get_thread(task_id) if m["kind"] == "result"]
    assert results
    text = results[0]["text"]
    assert text.startswith("CHANGES REQUESTED")
    assert "CHANGES REQUESTED: CHANGES REQUESTED" not in text
    assert text == "CHANGES REQUESTED -- needs more test coverage"


def test_cross_post_to_the_origin_task_does_not_stutter_either(db_path):
    """The first fix stripped the duplicate verdict from the reviewer's own
    result line and missed the note sent to the task that opened the MR, so
    "APPROVED — APPROVED — ..." just moved there. Seen live on task-8cd1f641.
    """
    from core import mergerequests
    sessions.clear()
    origin = store.create_task("build the thing", repo="sisyphus")
    mr = mergerequests.create_mr(task_id=origin["id"], repo="sisyphus",
                                 source_branch="sisyphus/task-x", target_branch="main",
                                 title="A change")
    review = store.create_task(
        f"Review-of-MR: {mr['id']}\nOrigin-task: {origin['id']}\n\nReview it.",
        repo="sisyphus")
    token = sessions.mint(review["id"], "reviewer")

    _call(_client(), token, "approve", {"summary": "APPROVED — clean, tests pass"})

    notes = [m["text"] for m in store.get_thread(origin["id"]) if m["kind"] == "note"]
    verdicts = [n for n in notes if "review verdict" in n]
    assert verdicts, "the origin task never heard the verdict"
    assert "APPROVED — APPROVED" not in verdicts[0]
    assert "clean, tests pass" in verdicts[0]


def test_cross_post_keeps_a_plain_reason_intact(db_path):
    from core import mergerequests
    sessions.clear()
    origin = store.create_task("build the thing", repo="sisyphus")
    mr = mergerequests.create_mr(task_id=origin["id"], repo="sisyphus",
                                 source_branch="sisyphus/task-y", target_branch="main",
                                 title="A change")
    review = store.create_task(
        f"Review-of-MR: {mr['id']}\nOrigin-task: {origin['id']}\n\nReview it.",
        repo="sisyphus")
    token = sessions.mint(review["id"], "reviewer")

    _call(_client(), token, "request_changes", {"reason": "no tests for the new branch"})

    notes = [m["text"] for m in store.get_thread(origin["id"]) if m["kind"] == "note"]
    verdicts = [n for n in notes if "review verdict" in n]
    assert verdicts and "CHANGES REQUESTED — no tests for the new branch" in verdicts[0]


def test_propose_subtasks_posts_plan(db_path):
    task_id, token = _session(db_path, "explorer")
    out = _call(_client(), token, "propose_subtasks", {"items": [
        {"repo": "ios-app", "goal": "iOS impl"},
        {"repo": "android-app", "goal": "Android impl"},
    ]})
    assert out["result"]["isError"] is False
    plan = [m for m in store.get_thread(task_id) if m["kind"] == "plan"]
    assert plan and "[ios-app] iOS impl" in plan[0]["text"]
    assert "[android-app] Android impl" in plan[0]["text"]


def test_propose_subtasks_requires_items(db_path):
    _, token = _session(db_path, "explorer")
    out = _call(_client(), token, "propose_subtasks", {"items": []})
    assert out["result"]["isError"] is True


def test_propose_subtasks_denied_for_engineer(db_path):
    _, token = _session(db_path, "engineer")
    out = _call(_client(), token, "propose_subtasks", {"items": [{"goal": "x"}]})
    assert out["result"]["isError"] is True  # not in the engineer's tool allowlist


# ── orchestrator decision tools ─────────────────────────────────────────


def test_decide_tool_records_decision_on_session(db_path):
    task_id, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker",
                {"agent": "engineer", "instruction": "fix it", "why": "start"})
    assert out["result"]["isError"] is False
    dec = sessions.resolve(token)["decision"]
    assert dec["action"] == "spawn_worker"
    assert dec["role"] == "engineer"        # agent name lands in the Decision.role field
    assert dec["instruction"] == "fix it"


def test_decide_complete_records_done(db_path):
    task_id, token = _session(db_path, "orchestrator")
    _call(_client(), token, "decide_complete", {"why": "all children shipped"})
    assert sessions.resolve(token)["decision"]["action"] == "done"


def test_decide_spawn_worker_requires_agent(db_path):
    _, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker", {"agent": ""})
    assert out["result"]["isError"] is True


def test_worker_cannot_call_decision_tools(db_path):
    _, token = _session(db_path, "engineer")
    out = _call(_client(), token, "decide_complete", {"why": "x"})
    assert out["result"]["isError"] is True   # not in the engineer's allowlist
    assert "not permitted" in json.loads(out["result"]["content"][0]["text"])["error"]


def test_decide_spawn_worker_rejects_unknown_agent(db_path):
    """A hallucinated agent name is rejected in-tool so the orchestrator can
    re-pick (regression: v4-flash inventing 'code')."""
    task_id, token = _session(db_path, "orchestrator")
    out = _call(_client(), token, "decide_spawn_worker", {"agent": "code", "instruction": "x"})
    assert out["result"]["isError"] is True
    err = json.loads(out["result"]["content"][0]["text"])["error"]
    assert "unknown agent 'code'" in err and "engineer" in err
    assert sessions.resolve(token).get("decision") is None   # not recorded


# ── merge request tools ──────────────────────────────────────────────────


def test_list_mrs_returns_open(db_path):
    from core import mergerequests

    _grant("explorer", "list_mrs")
    task_id, token = _session(db_path, "explorer")
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test MR")
    out = _call(_client(), token, "list_mrs")
    result = json.loads(out["result"]["content"][0]["text"])
    assert isinstance(result, list)
    assert any(r["id"] == mr["id"] for r in result)


def test_list_mrs_repo_filter(db_path):
    from core import mergerequests

    _grant("explorer", "list_mrs")
    task_id, token = _session(db_path, "explorer")
    mr_a = mergerequests.create_mr(task_id, "repo-a", "branch-a", "main", "MR A")
    mr_b = mergerequests.create_mr(task_id, "repo-b", "branch-b", "main", "MR B")

    all_out = _call(_client(), token, "list_mrs")
    all_result = json.loads(all_out["result"]["content"][0]["text"])
    assert len(all_result) >= 2

    filt_out = _call(_client(), token, "list_mrs", {"repo": "repo-a"})
    filt_result = json.loads(filt_out["result"]["content"][0]["text"])
    assert any(r["id"] == mr_a["id"] for r in filt_result)
    assert not any(r["id"] == mr_b["id"] for r in filt_result)


def test_get_mr_diff_returns_patch(db_path):
    from core import attachments, mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test")
    attachments.save_text(task_id, "change.patch", "diff --git a/x b/x\n")
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert "diff" in result
    assert "diff --git" in result["diff"]
    assert result["source"] == "snapshot"


def test_get_mr_diff_fresh_from_clone(db_path, tmp_path):
    from core import mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)

    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=test@test",
                    "-c", "user.name=test", "commit", "--allow-empty", "-m", "base"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "sisyphus-task"],
                   capture_output=True)
    (repo / "file.txt").write_text("changed")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=test@test",
                    "-c", "user.name=test", "commit", "-m", "change"],
                   capture_output=True)

    mr = mergerequests.create_mr(
        task_id, "test-repo", "sisyphus-task", "main", "Test",
        bare_path=str(repo),
    )
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert "diff" in result
    assert result["source"] == "fresh"
    assert "file.txt" in result["diff"]


def test_get_mr_diff_fresh_fails_falls_back(db_path):
    from core import attachments, mergerequests

    _grant("explorer", "get_mr_diff")
    task_id, token = _session(db_path)
    mr = mergerequests.create_mr(task_id, "test-repo", "branch", "main", "Test",
                                 bare_path="/nonexistent/path")
    attachments.save_text(task_id, "change.patch", "snapshot diff\n")
    out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
    result = json.loads(out["result"]["content"][0]["text"])
    assert result["source"] == "snapshot"
    assert result["diff"] == "snapshot diff\n"


def _repo_with_a_branch(tmp_path):
    """A real bare-ish clone so diff_range can resolve a head SHA."""
    repo = tmp_path / "clone"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-b", "main"], capture_output=True)
    subprocess.run(git + ["-c", "user.email=t@t", "-c", "user.name=t",
                          "commit", "--allow-empty", "-m", "base"], capture_output=True)
    subprocess.run(git + ["checkout", "-b", "sisyphus-task"], capture_output=True)
    (repo / "file.txt").write_text("changed")
    subprocess.run(git + ["add", "file.txt"], capture_output=True)
    subprocess.run(git + ["-c", "user.email=t@t", "-c", "user.name=t",
                          "commit", "-m", "change"], capture_output=True)
    head = subprocess.run(git + ["rev-parse", "sisyphus-task"],
                          capture_output=True, text=True).stdout.strip()
    return repo, head


class TestTheReviewerCanSeeTheRecordedCheck:
    """A review-of-MR task is a DIFFERENT task from the one that produced the
    MR, so core.verify's summary — posted to the origin task's thread — never
    reached the reviewer reading it. The persona said "the recorded check is
    authoritative" to an agent that had no way to see it, and three of the
    eleven human questions in the ledger are a reviewer asking a human to
    adjudicate sandbox-only failures while a green verdict sat unread."""

    def _diff(self, db_path, tmp_path, **check):
        from core import mergerequests
        _grant("explorer", "get_mr_diff")
        task_id, token = _session(db_path)
        repo, head = _repo_with_a_branch(tmp_path)
        mr = mergerequests.create_mr(task_id, "test-repo", "sisyphus-task", "main",
                                     "Test", bare_path=str(repo))
        if check:
            mergerequests.set_check_result(mr["id"], **check)
        out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
        return json.loads(out["result"]["content"][0]["text"]), head

    def test_a_passing_check_for_this_commit_is_handed_over_as_authoritative(
            self, db_path, tmp_path):
        from core import mergerequests
        _grant("explorer", "get_mr_diff")
        task_id, token = _session(db_path)
        repo, head = _repo_with_a_branch(tmp_path)
        mr = mergerequests.create_mr(task_id, "test-repo", "sisyphus-task", "main",
                                     "Test", bare_path=str(repo))
        mergerequests.set_check_result(mr["id"], "passed", output="1087 passed",
                                       command="pytest -q", head=head)

        out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
        chk = json.loads(out["result"]["content"][0]["text"])["recorded_check"]

        assert chk["status"] == "passed"
        assert chk["covers_head"] is True
        assert chk["command"] == "pytest -q"
        assert "1087 passed" in chk["output_tail"]
        assert "authoritative" in chk["meaning"]

    def test_a_check_from_before_the_last_commit_does_not_cover_the_diff(
            self, db_path, tmp_path):
        """The whole point of a machine verdict is that it cannot be stretched
        to cover work it never saw."""
        result, _ = self._diff(db_path, tmp_path, status="passed",
                               output="ok", command="pytest -q",
                               head="0000000000000000000000000000000000000000")
        chk = result["recorded_check"]
        assert chk["covers_head"] is False
        assert "does NOT cover" in chk["meaning"]

    def test_a_verdict_that_never_recorded_its_commit_cannot_be_stretched(
            self, db_path, tmp_path):
        """A check that ran before the check_head column existed (every MR
        checked before 2026-08-24) carries no commit at all. That is a hole in
        our record, not proof the branch moved — the reviewer must re-run the
        check instead of being told the verdict is stale."""
        result, _ = self._diff(db_path, tmp_path, status="passed",
                               output="ok", command="pytest -q")
        chk = result["recorded_check"]
        assert chk["covers_head"] is False
        assert "was never recorded" in chk["meaning"]
        assert "coverage cannot be determined" in chk["meaning"]
        assert "re-run the check" in chk["meaning"]
        assert "unknown commit" not in chk["meaning"]

    def test_no_recorded_check_never_reads_as_a_pass(self, db_path, tmp_path):
        result, _ = self._diff(db_path, tmp_path)
        chk = result["recorded_check"]
        assert chk["status"] == "none"
        assert "pass" not in chk["meaning"].lower() or "no machine verdict" in chk["meaning"]

    def test_a_failing_check_is_not_the_reviewers_to_dismiss(self, db_path, tmp_path):
        from core import mergerequests
        _grant("explorer", "get_mr_diff")
        task_id, token = _session(db_path)
        repo, head = _repo_with_a_branch(tmp_path)
        mr = mergerequests.create_mr(task_id, "test-repo", "sisyphus-task", "main",
                                     "Test", bare_path=str(repo))
        mergerequests.set_check_result(mr["id"], "failed", output="3 failed",
                                       command="pytest -q", head=head)

        out = _call(_client(), token, "get_mr_diff", {"mr_id": mr["id"]})
        chk = json.loads(out["result"]["content"][0]["text"])["recorded_check"]

        assert chk["status"] == "failed" and chk["covers_head"] is True
        assert "Request changes" in chk["meaning"]


def test_get_mr_diff_missing_errors(db_path):
    _grant("explorer", "get_mr_diff")
    _, token = _session(db_path)
    out = _call(_client(), token, "get_mr_diff", {"mr_id": "mr-nonexistent"})
    assert out["result"]["isError"] is True


def test_list_mrs_requires_session(db_path):
    _grant("explorer", "list_mrs")
    out = _call(_client(), None, "list_mrs")
    assert out["id"] == 1
    assert "error" in out


# ── review task cross-posting ───────────────────────────────────────────


def test_approve_on_review_task_sets_review_status_and_crossposts(db_path):
    from core import mergerequests, reviews, sessions

    origin = store.create_task("build feature X", repo="sisyphus", agent="engineer")
    mr = mergerequests.create_mr(
        task_id=origin["id"], repo="sisyphus",
        source_branch="sisyphus/feat", target_branch="main",
        title="Add feature X",
    )
    review = reviews.ensure_review_task(mr)
    assert review is not None

    sessions.clear()
    token = sessions.mint(review["id"], "reviewer")
    out = _call(_client(), token, "approve", {"summary": "looks great"})
    assert out["result"]["isError"] is False

    got = mergerequests.get_mr(mr["id"])
    assert got["review_status"] == "approved"

    origin_thread = store.get_thread(origin["id"])
    notes = [m for m in origin_thread if m["author"] == "system" and m["kind"] == "note"]
    assert any("APPROVED" in m["text"] and "looks great" in m["text"] for m in notes)


def test_request_changes_on_review_task_sets_review_status_and_crossposts(db_path):
    from core import mergerequests, reviews, sessions

    origin = store.create_task("build feature Y", repo="sisyphus", agent="engineer")
    mr = mergerequests.create_mr(
        task_id=origin["id"], repo="sisyphus",
        source_branch="sisyphus/feat2", target_branch="main",
        title="Add feature Y",
    )
    review = reviews.ensure_review_task(mr)
    assert review is not None

    sessions.clear()
    token = sessions.mint(review["id"], "reviewer")
    out = _call(_client(), token, "request_changes", {"reason": "needs tests"})
    assert out["result"]["isError"] is False

    got = mergerequests.get_mr(mr["id"])
    assert got["review_status"] == "changes_requested"

    origin_thread = store.get_thread(origin["id"])
    notes = [m for m in origin_thread if m["author"] == "system" and m["kind"] == "note"]
    assert any("CHANGES REQUESTED" in m["text"] and "needs tests" in m["text"] for m in notes)


def test_open_mr_fires_review_hook(db_path):
    from core import mergerequests, reviews, sessions

    sessions.clear()
    task = store.create_task("build Z", repo="sisyphus")
    token = sessions.mint(task["id"], "engineer")
    c = _client()

    from unittest.mock import patch

    published_mr_id = None

    def _pub(session, title, desc):
        nonlocal published_mr_id
        from core import attachments
        attachments.save_text(session["task_id"], "change.patch", "diff content\n")
        existing = mergerequests.find_open_mr(
            session["task_id"], "sisyphus/task-1", "main")
        if existing:
            mr = mergerequests.update_mr(existing["id"], title=title)
        else:
            mr = mergerequests.create_mr(
                task_id=session["task_id"], repo="test-repo",
                source_branch="sisyphus/task-1", target_branch="main",
                title=title, bare_path="/tmp/bare",
                base_branch="main", source_path="/tmp/src",
            )
        published_mr_id = mr["id"]
        return {"ok": True, "platform": "local", "branch": "sisyphus/task-1",
                "stat": " file.py | 1 +", "patch_file": "change.patch",
                "mr_id": mr["id"]}

    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="a" * 40):
        _call(c, token, "commit", {"message": "fix: review hook"})

    emitted = []
    with patch("core.providers.publish_change", side_effect=_pub), \
         patch("core.orchestrator.emit", side_effect=lambda t, e: emitted.append((t, e))):
        ok = _call(c, token, "open_mr", {"title": "Review Hook Test"})
        assert ok["result"]["isError"] is False
        # The hook runs off the request path now — wait for it before asserting.
        import web.agent_mcp as am
        am._last_review_hook.join(timeout=10)

    mr = mergerequests.get_mr(published_mr_id)
    assert mr["review_status"] == "pending"

    review_tasks = [t for t in store.list_tasks()
                    if t.get("agent") == "mr-reviewer"
                    and f"Review-of-MR: {published_mr_id}" in t["goal"]]
    assert len(review_tasks) == 1

    # Created is not enough — an un-emitted task never reaches the dispatch
    # queue and sits `active` with an empty thread forever (live incident
    # task-246eb605). The hook must put it on the queue.
    assert (review_tasks[0]["id"], "created") in emitted


def test_second_decision_in_one_pass_is_rejected(db_path):
    """Exactly one decision per decider pass: the first wins, a second decide_*
    call errors instead of silently overwriting (live incident task-1e23e9e8:
    three overwriting spawns ended as a bogus ask_human)."""
    _, token = _session(db_path, "orchestrator")
    first = _call(_client(), token, "decide_spawn_worker",
                  {"agent": "engineer", "instruction": "fix it", "why": "start"})
    assert first["result"]["isError"] is False
    second = _call(_client(), token, "decide_ask_human", {"question": "help?"})
    assert second["result"]["isError"] is True
    assert sessions.resolve(token)["decision"]["action"] == "spawn_worker"


# ── a task's OWN reviewer must also reach the MR row ─────────────────────


def test_own_reviewer_approve_reaches_the_tasks_open_mr(db_path):
    """The review-gated merge reads mr.review_status; only the Review-of-MR
    path recorded it, so a task's own reviewer approving on the thread left
    the MR `pending` and the gate refused the human's merge (mr-9b6852f8)."""
    from core import mergerequests, sessions

    task = store.create_task("build feature Z", repo="sisyphus", agent="engineer")
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="sisyphus",
        source_branch="sisyphus/feat-z", target_branch="main",
        title="Add feature Z",
    )
    sessions.clear()
    token = sessions.mint(task["id"], "reviewer")
    out = _call(_client(), token, "approve", {"summary": "clean, tests pass"})
    assert out["result"]["isError"] is False
    assert mergerequests.get_mr(mr["id"])["review_status"] == "approved"


def test_own_reviewer_request_changes_reaches_the_tasks_open_mr(db_path):
    from core import mergerequests, sessions

    task = store.create_task("build feature W", repo="sisyphus", agent="engineer")
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="sisyphus",
        source_branch="sisyphus/feat-w", target_branch="main",
        title="Add feature W",
    )
    sessions.clear()
    token = sessions.mint(task["id"], "reviewer")
    out = _call(_client(), token, "request_changes", {"reason": "needs tests"})
    assert out["result"]["isError"] is False
    assert mergerequests.get_mr(mr["id"])["review_status"] == "changes_requested"


def test_own_reviewer_verdict_leaves_a_settled_mr_alone(db_path):
    """A merged/closed MR's verdict is history — a late reviewer must not
    rewrite it."""
    from core import mergerequests, sessions

    task = store.create_task("build feature V", repo="sisyphus", agent="engineer")
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="sisyphus",
        source_branch="sisyphus/feat-v", target_branch="main",
        title="Add feature V",
    )
    mergerequests.set_review_status(mr["id"], "approved")
    mergerequests.mark_merged(mr["id"], "abc123")
    sessions.clear()
    token = sessions.mint(task["id"], "reviewer")
    _call(_client(), token, "request_changes", {"reason": "too late"})
    assert mergerequests.get_mr(mr["id"])["review_status"] == "approved"


def test_open_mr_replies_before_a_slow_check_finishes(db_path):
    """The failure this guards (task-60088d8f): ensure_review_task ran a 150s
    check INSIDE the open_mr MCP request; the client timed out at ~60s, dropped
    the server, and the engineer retried open_mr 160 times against an empty
    roster until the total timeout. The reply must not wait for the check."""
    import threading
    import time

    import web.agent_mcp as am
    from core import mergerequests, sessions

    _grant("engineer", "commit", "open_mr")
    c = _client()
    task = store.create_task("slow check", repo="test-repo")
    sessions.clear()
    token = sessions.mint(task["id"], "engineer", worktree="/tmp/wt")

    def _pub(session, title, description):
        mergerequests.init_mr_table()
        mr = mergerequests.create_mr(
            task_id=session["task_id"], repo="test-repo",
            source_branch="sisyphus/task-slow", target_branch="main",
            title=title, bare_path="/tmp/bare",
            base_branch="main", source_path="/tmp/src")
        return {"ok": True, "platform": "local", "branch": "sisyphus/task-slow",
                "stat": " f | 1 +", "patch_file": "change.patch", "mr_id": mr["id"]}

    check_started = threading.Event()
    check_release = threading.Event()

    def _slow_review_task(mr):
        check_started.set()
        assert check_release.wait(timeout=10), "reply never arrived — hook is synchronous again"
        return None

    with patch("core.git_ops.is_git_repo", return_value=True), \
         patch("core.git_ops.commit_all", return_value="b" * 40):
        _call(c, token, "commit", {"message": "x"})

    with patch("core.providers.publish_change", side_effect=_pub), \
         patch("core.reviews.ensure_review_task", side_effect=_slow_review_task):
        started = time.monotonic()
        ok = _call(c, token, "open_mr", {"title": "Slow"})
        elapsed = time.monotonic() - started
        # The reply is in hand while the "check" is still blocked.
        assert ok["result"]["isError"] is False
        assert check_started.wait(timeout=10)
        check_release.set()
        am._last_review_hook.join(timeout=10)
    assert elapsed < 5, f"open_mr reply waited on the check ({elapsed:.1f}s)"
