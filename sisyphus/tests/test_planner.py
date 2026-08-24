"""Mission Planner core (core/planner.py) — mocked runner, no tokens spent."""
import json

import pytest

from core import planner, store


def _emit(monkeypatch):
    calls = []
    monkeypatch.setattr("core.orchestrator.emit", lambda tid, ev: calls.append((tid, ev)))
    return calls


def test_plan_crud(db_path):
    p = planner.create_plan("Ship X", repo="testrepo")
    assert p["id"].startswith("pl-") and p["repo"] == "testrepo"
    assert planner.get_plan(p["id"])["title"] == "Ship X"
    planner.update_plan(p["id"], title="Ship X v2")
    assert planner.get_plan(p["id"])["title"] == "Ship X v2"
    assert planner.latest_plan()["id"] == p["id"]
    assert planner.delete_plan(p["id"]) is True
    assert planner.get_plan(p["id"]) is None


def test_items_and_dag_state(db_path):
    p = planner.create_plan("p")
    a = planner.add_item(p["id"], "fix", position=0)
    b = planner.add_item(p["id"], "review", after=[a["id"]], position=1)
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.item_state(a, by) == "ready"      # no deps
    assert planner.item_state(b, by) == "blocked"    # a not done
    assert not planner.releasable(b, by)
    # finish a → b becomes ready
    planner.update_item(a["id"], status="done")
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.releasable(planner.get_item(b["id"]), by)


def test_cycle_detection(db_path):
    """has_cycle correctly detects cycles in dependency graphs."""
    p = planner.create_plan("p")
    a = planner.add_item(p["id"], "a")
    b = planner.add_item(p["id"], "b", after=[a["id"]])
    # Acyclic: a -> b
    assert planner.has_cycle(planner.list_items(p["id"])) is False
    # Try to create cycle a→b→a; guard prevents it
    with pytest.raises(ValueError, match="dependency cycle"):
        planner.update_item(a["id"], after=[b["id"]])
    # Graph is still acyclic
    assert planner.has_cycle(planner.list_items(p["id"])) is False


def test_plan_turn_saves_brief(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    brief_json = json.dumps({
        "approach": "Do the thing.",
        "diagram": "graph TD; A-->B",
        "steps": [
            {"goal": "fix bug", "repo": "testrepo", "agent": "engineer", "after": []},
            {"goal": "review", "repo": "testrepo", "agent": "reviewer", "after": [0]},
            {"goal": "bad", "agent": "ghost", "after": [99]},
        ],
        "risks": ["might break X"], "questions": ["which branch?"], "affected": ["calc.py"],
    })
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: brief_json)
    b = planner.plan_turn(p["id"], "fix the offline bug")
    assert b["approach"] == "Do the thing." and b["diagram"].startswith("graph")
    assert len(b["steps"]) == 3
    assert b["steps"][2]["agent"] == ""          # unknown agent dropped
    assert planner.get_plan(p["id"])["brief_obj"]["risks"] == ["might break X"]


def test_plan_turn_records_conversation(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    planner.add_message(p["id"], "user", "add offline mode")   # simulate start_plan_turn
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "reply": "Got it — sketched the offline sync flow on the canvas.",
        "approach": "cache-first", "diagram": "", "steps": [],
        "risks": [], "questions": [], "affected": [],
    }))
    b = planner.plan_turn(p["id"], "add offline mode")
    assert b["reply"].startswith("Got it")
    msgs = planner.list_messages(p["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == b["reply"]


def test_plan_turn_unparseable_raises(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], brief=json.dumps({"approach": "keep me", "steps": [],
        "diagram": "", "risks": [], "questions": [], "affected": []}))
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: "sorry, I can't help with that")
    with pytest.raises(ValueError):
        planner.plan_turn(p["id"], "do something")
    # existing brief is preserved, not clobbered with an empty shell
    assert planner.get_plan(p["id"])["brief_obj"]["approach"] == "keep me"


def test_integration_branch_flows_code_to_next_step(db_path, tmp_path, monkeypatch):
    """Real git, no AI: a step's code merged into the plan branch is present when
    the next step is cut from that branch — the mechanism that lets dependent
    steps see each other's work — while the repo's main stays untouched."""
    import subprocess
    from core import git_ops, mergerequests, repos

    def git(*a, cwd, **kw):
        return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, **kw)

    src = tmp_path / "src"; src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    git("config", "user.email", "t@t", cwd=str(src)); git("config", "user.name", "t", cwd=str(src))
    (src / "README.md").write_text("base\n")
    git("add", "-A", cwd=str(src)); git("commit", "-m", "init", cwd=str(src))

    monkeypatch.setattr(repos, "get_repo",
                        lambda name: {"name": name, "source": str(src), "branch": "main"})
    mergerequests.init_mr_table()

    plan_id = "pl-flow01"
    br = planner._ensure_plan_branch(plan_id, "r")
    assert br == f"plan/{plan_id}"                         # plan branch stood up in source

    # step 1: clone, cut a task branch off the plan branch, add a file, register an MR
    bare1 = tmp_path / "bare1.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(bare1)], capture_output=True)
    t1 = git_ops.ensure_task_branch(str(bare1), "task-flow1", br)
    wt1 = tmp_path / "wt1"
    subprocess.run(["git", "-C", str(bare1), "worktree", "add", str(wt1), t1], capture_output=True)
    (wt1 / "step1.py").write_text("def one(): return 'STEP1'\n")
    git("add", "-A", cwd=str(wt1))
    git("-c", "user.email=s@s", "-c", "user.name=s", "commit", "-m", "step1", cwd=str(wt1))
    mergerequests.create_mr(task_id="task-flow1", repo="r", source_branch=t1, target_branch=br,
                            title="s1", bare_path=str(bare1), base_branch=br, source_path=str(src))

    planner._integrate_into_plan_branch("task-flow1", plan_id)   # ← the merge-forward

    # the plan branch in source now carries step 1's file …
    assert git("show", f"{br}:step1.py", cwd=str(src)).returncode == 0
    # … so a fresh clone cut off the plan branch (step 2) sees it
    bare2 = tmp_path / "bare2.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(bare2)], capture_output=True)
    t2 = git_ops.ensure_task_branch(str(bare2), "task-flow2", br)
    seen = git("show", f"{t2}:step1.py", cwd=str(bare2))
    assert seen.returncode == 0 and "STEP1" in seen.stdout   # code flowed to the next step
    # main never received it
    assert git("show", "main:step1.py", cwd=str(src)).returncode != 0


def _conflict_repo(tmp_path):
    """A repo where branch A and B change the same line (x.py) — merging B into A
    conflicts. Returns (src_path, git_fn)."""
    import subprocess
    src = tmp_path / "src"; src.mkdir()
    def git(*a, cwd=str(src)):
        return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True)
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (src / "x.py").write_text("base\n"); git("add", "-A"); git("commit", "-m", "i")
    git("branch", "A", "main"); git("branch", "B", "main")
    for br, content in (("A", "AAA\n"), ("B", "BBB\n")):
        wt = tmp_path / ("wt" + br)
        subprocess.run(["git", "-C", str(src), "worktree", "add", str(wt), br], capture_output=True)
        (wt / "x.py").write_text(content)
        git("add", "-A", cwd=str(wt))
        git("-c", "user.email=s@s", "-c", "user.name=s", "commit", "-m", br, cwd=str(wt))
        subprocess.run(["git", "-C", str(src), "worktree", "remove", "--force", str(wt)], capture_output=True)
    return src, git


def test_merge_resolving_auto_resolves_conflict(db_path, tmp_path):
    from core import mergerequests
    src, git = _conflict_repo(tmp_path)
    mergerequests.init_mr_table()
    mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="B", target_branch="A",
                                 title="", bare_path=str(src), base_branch="A", source_path=str(src))
    seen = {}
    def resolver(path, text):
        seen["path"] = path
        assert "<<<<<<<" in text            # the conflicted content is handed over
        return "MERGED\n"
    res = mergerequests.merge_resolving(mr, "A", resolver)
    assert res["ok"] and res["resolved"] == ["x.py"] and seen["path"] == "x.py"
    assert git("show", "A:x.py").stdout == "MERGED\n"     # A now carries the resolution


def test_merge_resolving_aborts_when_unresolvable(db_path, tmp_path):
    from core import mergerequests
    src, git = _conflict_repo(tmp_path)
    mergerequests.init_mr_table()
    mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="B", target_branch="A",
                                 title="", bare_path=str(src), base_branch="A", source_path=str(src))
    res = mergerequests.merge_resolving(mr, "A", lambda p, t: None)   # resolver gives up
    assert not res["ok"]
    assert git("show", "A:x.py").stdout == "AAA\n"        # A left untouched


def test_ai_conflict_resolver_guards_bad_output(db_path, monkeypatch):
    # a resolution that still contains markers, or no JSON, is rejected (→ None)
    monkeypatch.setattr("core.ai_assist.run_prompt",
                        lambda p, **k: '{"resolved": "<<<<<<< still conflicted"}')
    assert planner._ai_conflict_resolver("x.py", "conflict") is None
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda p, **k: "no json here")
    assert planner._ai_conflict_resolver("x.py", "conflict") is None
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda p, **k: '{"resolved": "clean()\\n"}')
    assert planner._ai_conflict_resolver("x.py", "conflict") == "clean()\n"


def test_open_plan_mr_lands_the_whole_feature(db_path, tmp_path, monkeypatch):
    """When the plan branch has diverged from the default, one MR (plan → default)
    is opened to land the assembled feature — idempotently, and not before there's
    anything to land."""
    import subprocess
    from core import repos, mergerequests

    def git(*a, cwd):
        return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True)

    src = tmp_path / "src"; src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    git("config", "user.email", "t@t", cwd=str(src)); git("config", "user.name", "t", cwd=str(src))
    (src / "a").write_text("1\n"); git("add", "-A", cwd=str(src)); git("commit", "-m", "i", cwd=str(src))

    p = planner.create_plan("Feat", repo="r")
    monkeypatch.setattr(repos, "get_repo",
                        lambda name: {"name": name, "source": str(src), "branch": "main"})
    mergerequests.init_mr_table()
    br = planner._ensure_plan_branch(p["id"], "r")

    planner._open_plan_mr(p["id"])                     # nothing landed yet → no MR
    assert not any(m["source_branch"] == br for m in mergerequests.list_mrs())

    # a commit lands on the plan branch (what integration does)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(src), "worktree", "add", str(wt), br], capture_output=True)
    (wt / "feat.py").write_text("x\n")
    git("add", "-A", cwd=str(wt))
    git("-c", "user.email=s@s", "-c", "user.name=s", "commit", "-m", "feat", cwd=str(wt))

    planner._open_plan_mr(p["id"])
    mrs = [m for m in mergerequests.list_mrs() if m["source_branch"] == br]
    assert len(mrs) == 1 and mrs[0]["target_branch"] == "main"
    planner._open_plan_mr(p["id"])                     # idempotent
    assert len([m for m in mergerequests.list_mrs() if m["source_branch"] == br]) == 1


def test_convert_to_drafts_maps_deps(db_path, monkeypatch):
    p = planner.create_plan("p", repo="testrepo")
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "approach": "", "diagram": "", "risks": [], "questions": [], "affected": [],
        "steps": [
            {"goal": "step A", "repo": "testrepo", "agent": "", "after": []},
            {"goal": "step B", "repo": "testrepo", "agent": "", "after": [0]},
        ],
    }))
    planner.plan_turn(p["id"], "go")
    items = planner.convert_to_drafts(p["id"])
    assert len(items) == 2 and items[0]["goal"] == "step A"
    assert items[1]["after"] == [items[0]["id"]]     # index 0 → A's id
    assert not planner.has_cycle(items)


def test_launch_item(db_path, monkeypatch):
    calls = _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "do A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "do B", after=[a["id"]])

    # b is blocked → launch refused
    with pytest.raises(ValueError):
        planner.launch_item(b["id"])

    la = planner.launch_item(a["id"])
    assert la["status"] == "launched" and la["task_id"]
    assert (la["task_id"], "created") in calls
    assert store.get_task(la["task_id"])["goal"] == "do A"

    # a's task done → planner advances the item; b becomes releasable
    planner.on_task_terminal(la["task_id"], "done")
    assert planner.get_item(a["id"])["status"] == "done"
    by = {x["id"]: x for x in planner.list_items(p["id"])}
    assert planner.releasable(planner.get_item(b["id"]), by)


def test_on_task_terminal_failed(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "do A", repo="testrepo")
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "failed")
    assert planner.get_item(a["id"])["status"] == "failed"


def test_auto_release_cascades(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_release=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    c = planner.add_item(p["id"], "C", repo="testrepo", after=[b["id"]])
    la = planner.launch_item(a["id"])

    # A done → B is auto-launched (not left as a ready draft)
    planner.on_task_terminal(la["task_id"], "done")
    b = planner.get_item(b["id"])
    assert b["status"] == "launched" and b["task_id"]
    assert planner.get_item(c["id"])["status"] == "draft"   # still blocked by B

    # B done → C auto-launches too — the whole chain runs hands-off
    planner.on_task_terminal(b["task_id"], "done")
    assert planner.get_item(c["id"])["status"] == "launched"


def test_auto_release_off_by_default(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "A", repo="testrepo")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "done")
    assert planner.get_item(b["id"])["status"] == "draft"   # ready, but not auto-launched


def test_plan_branch_name(db_path):
    assert planner.plan_branch("pl-abc123") == "plan/pl-abc123"


def test_launch_cuts_from_plan_branch(db_path, monkeypatch):
    # a repo-backed step is cut from the plan's integration branch
    _emit(monkeypatch)
    monkeypatch.setattr("core.planner._ensure_plan_branch", lambda pid, repo: f"plan/{pid}")
    p = planner.create_plan("p", repo="testrepo")
    a = planner.add_item(p["id"], "do A", repo="testrepo", agent="engineer")
    la = planner.launch_item(a["id"])
    assert store.get_task(la["task_id"])["base_branch"] == f"plan/{p['id']}"


def test_repoless_step_has_no_base_branch(db_path, monkeypatch):
    _emit(monkeypatch)
    p = planner.create_plan("p")                      # no repo
    a = planner.add_item(p["id"], "explore", agent="explorer")
    la = planner.launch_item(a["id"])
    assert store.get_task(la["task_id"])["base_branch"] is None


def test_integrate_runs_on_done_then_releases_onto_plan_branch(db_path, monkeypatch):
    _emit(monkeypatch)
    monkeypatch.setattr("core.planner._ensure_plan_branch", lambda pid, repo: f"plan/{pid}")
    integrated = []
    monkeypatch.setattr("core.planner._integrate_into_plan_branch",
                        lambda tid, pid: integrated.append(tid))
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_release=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "done")
    assert integrated == [la["task_id"]]              # step integrated into plan branch
    b = planner.get_item(b["id"])
    assert b["status"] == "launched"                  # dependent auto-released
    assert store.get_task(b["task_id"])["base_branch"] == f"plan/{p['id']}"  # onto the same branch


def test_auto_merge_invoked_on_done(db_path, monkeypatch):
    _emit(monkeypatch)
    merged = []
    monkeypatch.setattr("core.planner._auto_merge_task", lambda tid: merged.append(tid))
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_merge=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    la = planner.launch_item(a["id"])
    planner.on_task_terminal(la["task_id"], "done")
    assert merged == [la["task_id"]]

    # failed task → no merge attempt
    merged.clear()
    b = planner.add_item(p["id"], "B", repo="testrepo")
    lb = planner.launch_item(b["id"])
    planner.on_task_terminal(lb["task_id"], "failed")
    assert merged == []


def _open_mr_with_review(db_path, review_status):
    """A real task + an open MR whose review_status is set (or left NULL)."""
    from core import mergerequests
    task = store.create_task("go", repo="r")
    mr = mergerequests.create_mr(task_id=task["id"], repo="r",
                                 source_branch=f"sisyphus/{task['id']}",
                                 target_branch="main", title="t",
                                 bare_path="/tmp/bare", base_branch="main",
                                 source_path="/tmp/src")
    if review_status is not None:
        mergerequests.set_review_status(mr["id"], review_status)
    return task["id"], mr


@pytest.mark.parametrize("review_status", ["changes_requested", "pending", None])
def test_auto_merge_skipped_when_review_gated(db_path, monkeypatch, review_status):
    """An MR without a recorded approval is never auto-merged: no git changes,
    the MR stays open, and a clear note lands on the thread. NULL is in this
    list on purpose — it means nobody ever reviewed, and it used to sail through
    a gate that only knew how to stop the two verdicts it could name."""
    from core import mergerequests
    task_id, mr = _open_mr_with_review(db_path, review_status)
    merged, marked, cleaned = [], [], []
    monkeypatch.setattr("core.mergerequests.check_mergeability",
                        lambda *a: {"ok": True, "mergeable": True})
    monkeypatch.setattr("core.mergerequests.merge_branch", lambda m, t: merged.append(t))
    monkeypatch.setattr("core.mergerequests.mark_merged", lambda mid, sha: marked.append(mid))
    monkeypatch.setattr("core.worker.cleanup_task", lambda tid: cleaned.append(tid))

    planner._auto_merge_task(task_id)

    assert merged == [] and marked == [] and cleaned == []
    assert mergerequests.get_mr(mr["id"])["status"] == "open"   # MR left open
    notes = [m for m in store.get_thread(task_id) if m["kind"] == "note"]
    expected = (f"auto-merge skipped: review is {review_status}" if review_status
                else "auto-merge skipped: no review verdict is recorded")
    assert any(expected in n["text"] for n in notes)


def test_auto_merge_skipped_when_the_daemons_own_check_failed(db_path, monkeypatch):
    """The machine contradicting the approval outranks the approval."""
    from core import mergerequests
    task_id, mr = _open_mr_with_review(db_path, "approved")
    mergerequests.set_check_result(mr["id"], "failed", output="3 failed",
                                   command="pytest -q")
    monkeypatch.setattr("core.mergerequests.check_mergeability",
                        lambda *a: {"ok": True, "mergeable": True})

    planner._auto_merge_task(task_id)

    assert mergerequests.get_mr(mr["id"])["status"] == "open"
    notes = [m["text"] for m in store.get_thread(task_id) if m["kind"] == "note"]
    assert any("check FAILED" in n for n in notes), notes


@pytest.mark.parametrize("review_status", ["approved"])
def test_auto_merge_proceeds_when_review_allows(db_path, monkeypatch, review_status):
    """A recorded approval — and only that — auto-merges."""
    from core import mergerequests
    task_id, mr = _open_mr_with_review(db_path, review_status)
    cleaned = []
    monkeypatch.setattr("core.mergerequests.check_mergeability",
                        lambda *a: {"ok": True, "mergeable": True})
    monkeypatch.setattr("core.mergerequests.merge_branch",
                        lambda m, t: {"ok": True, "sha": "deadbeef", "target": t})
    monkeypatch.setattr("core.worker.cleanup_task", lambda tid: cleaned.append(tid))

    planner._auto_merge_task(task_id)

    assert cleaned == [task_id]
    merged = mergerequests.get_mr(mr["id"])
    assert merged["status"] == "merged" and merged["merge_sha"] == "deadbeef"


def test_auto_merge_skip_keeps_plan_progression(db_path, monkeypatch):
    """A gated MR skips auto-merge without stalling the plan: the step still
    integrates into the plan branch and its dependent still auto-releases — the
    same terminal handling as when auto_merge is disabled for the step."""
    from core import mergerequests
    _emit(monkeypatch)
    monkeypatch.setattr("core.planner._ensure_plan_branch", lambda pid, repo: f"plan/{pid}")
    integrated = []
    monkeypatch.setattr("core.planner._integrate_into_plan_branch",
                        lambda tid, pid: integrated.append(tid))
    p = planner.create_plan("p", repo="testrepo")
    planner.update_plan(p["id"], auto_merge=1, auto_release=1)
    a = planner.add_item(p["id"], "A", repo="testrepo", agent="engineer")
    b = planner.add_item(p["id"], "B", repo="testrepo", after=[a["id"]])
    la = planner.launch_item(a["id"])
    mr = mergerequests.create_mr(task_id=la["task_id"], repo="r",
                                 source_branch=f"sisyphus/{la['task_id']}",
                                 target_branch="main", title="t",
                                 bare_path="/tmp/bare", base_branch="main",
                                 source_path="/tmp/src")
    mergerequests.set_review_status(mr["id"], "changes_requested")

    planner.on_task_terminal(la["task_id"], "done")

    assert integrated == [la["task_id"]]                       # step still integrated
    assert planner.get_item(b["id"])["status"] == "launched"   # dependent still auto-released
    assert mergerequests.get_mr(mr["id"])["status"] == "open"  # MR left open, nothing merged
    notes = [m for m in store.get_thread(la["task_id"]) if m["kind"] == "note"]
    assert any("auto-merge skipped" in n["text"] for n in notes)


def test_convert_to_drafts_rejects_cyclic_graph(db_path, monkeypatch):
    """convert_to_drafts raises ValueError if the brief's steps contain a cycle,
    and creates no items."""
    p = planner.create_plan("p", repo="testrepo")
    # Brief with a cycle: step 0 -> step 1 -> step 0
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "approach": "", "diagram": "", "risks": [], "questions": [], "affected": [],
        "steps": [
            {"goal": "step A", "repo": "testrepo", "agent": "", "after": [1]},
            {"goal": "step B", "repo": "testrepo", "agent": "", "after": [0]},
        ],
    }))
    planner.plan_turn(p["id"], "go")
    # Try to convert — should raise and leave no draft items
    with pytest.raises(ValueError, match="dependency cycle"):
        planner.convert_to_drafts(p["id"])
    # Verify no draft items were created
    items = planner.list_items(p["id"])
    assert len(items) == 0


def test_convert_to_drafts_acyclic_graph_still_works(db_path, monkeypatch):
    """convert_to_drafts with a valid acyclic graph succeeds and creates items."""
    p = planner.create_plan("p", repo="testrepo")
    # Brief with no cycle: step 0 -> step 1 -> step 2
    monkeypatch.setattr("core.ai_assist.run_prompt", lambda pr, **k: json.dumps({
        "approach": "", "diagram": "", "risks": [], "questions": [], "affected": [],
        "steps": [
            {"goal": "step A", "repo": "testrepo", "agent": "", "after": []},
            {"goal": "step B", "repo": "testrepo", "agent": "", "after": [0]},
            {"goal": "step C", "repo": "testrepo", "agent": "", "after": [1]},
        ],
    }))
    planner.plan_turn(p["id"], "go")
    items = planner.convert_to_drafts(p["id"])
    assert len(items) == 3
    assert items[0]["goal"] == "step A"
    assert items[1]["goal"] == "step B" and items[1]["after"] == [items[0]["id"]]
    assert items[2]["goal"] == "step C" and items[2]["after"] == [items[1]["id"]]
    assert not planner.has_cycle(items)


def test_update_item_rejects_cyclic_dependency(db_path):
    """update_item raises ValueError if the update would introduce a cycle."""
    p = planner.create_plan("p")
    a = planner.add_item(p["id"], "a")
    b = planner.add_item(p["id"], "b", after=[a["id"]])
    # Try to create cycle a->b->a; should raise
    with pytest.raises(ValueError, match="dependency cycle"):
        planner.update_item(a["id"], after=[b["id"]])
    # Verify a's dependencies weren't changed
    assert planner.get_item(a["id"])["after"] == []


def test_merge_resolving_into_the_checked_out_branch(db_path, tmp_path):
    """The real-world case: an MR targets `main`, which is the branch the user
    has checked out. This used to fail every time with "fatal: 'main' is already
    used by worktree at ..." because the function always took the throwaway-
    worktree path, which git refuses for an already-checked-out branch. The
    existing tests merged into `A`, which is never checked out, so the path an
    ordinary MR takes was untested.
    """
    from core import mergerequests
    src, git = _conflict_repo(tmp_path)
    # Make `main` conflict with B, and leave main checked out (it already is).
    git("merge", "--no-edit", "A")
    mergerequests.init_mr_table()
    mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="B", target_branch="main",
                                 title="", bare_path=str(src), base_branch="main", source_path=str(src))
    res = mergerequests.merge_resolving(mr, "main", lambda path, text: "MERGED\n")
    assert res["ok"], res.get("error")
    assert res["resolved"] == ["x.py"]
    assert git("show", "main:x.py").stdout == "MERGED\n"
    # The user's checkout is left on main, clean — not removed as a worktree.
    assert git("symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert git("status", "--porcelain").stdout.strip() == ""


def test_merge_resolving_refuses_dirty_checked_out_target(db_path, tmp_path):
    from core import mergerequests
    src, git = _conflict_repo(tmp_path)
    git("merge", "--no-edit", "A")
    (src / "x.py").write_text("uncommitted local edit\n")
    mergerequests.init_mr_table()
    mr = mergerequests.create_mr(task_id="t1", repo="r", source_branch="B", target_branch="main",
                                 title="", bare_path=str(src), base_branch="main", source_path=str(src))
    res = mergerequests.merge_resolving(mr, "main", lambda path, text: "MERGED\n")
    assert not res["ok"]
    assert "uncommitted" in res["error"]
    # The user's edit survives untouched.
    assert (src / "x.py").read_text() == "uncommitted local edit\n"
