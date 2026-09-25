"""AI-assisted conflict resolution for a merge request, from the MR view.

The resolver (core.mergerequests.ai_conflict_resolver) has worked for a long time but
was reachable only from plan-branch integration, so an ordinary conflicting MR
had no path to it — which is how a real bug in merge_resolving survived. These
tests exercise the route, never a model: the resolver is monkeypatched.
"""
import subprocess

from fastapi.testclient import TestClient

from core import mergerequests, store
from web.app import app


def _git(*a, cwd):
    return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True)


def _repo(tmp_path):
    """A repo checked out on `main` where branch B rewrites the same line of
    x.py that main does (→ conflict) and branch C adds a new file (→ clean).
    `main` being the checked-out branch is the ordinary MR case."""
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-b", "main", str(src)], capture_output=True)
    s = str(src)
    _git("config", "user.email", "t@t", cwd=s)
    _git("config", "user.name", "t", cwd=s)
    (src / "x.py").write_text("base\n")
    _git("add", "-A", cwd=s); _git("commit", "-m", "init", cwd=s)
    _git("branch", "B", "main", cwd=s)
    _git("branch", "C", "main", cwd=s)
    for br, fname, content in (("B", "x.py", "BBB\n"), ("C", "other.py", "hello\n")):
        wt = tmp_path / ("wt" + br)
        _git("worktree", "add", str(wt), br, cwd=s)
        (wt / fname).write_text(content)
        _git("add", "-A", cwd=str(wt))
        _git("-c", "user.email=s@s", "-c", "user.name=s", "commit", "-m", br, cwd=str(wt))
        _git("worktree", "remove", "--force", str(wt), cwd=s)
    # main diverges on the same line as B
    (src / "x.py").write_text("AAA\n")
    _git("add", "-A", cwd=s); _git("commit", "-m", "main moves", cwd=s)
    return src


def _mr(tmp_path, source="B"):
    src = _repo(tmp_path)
    task = store.create_task("do it", repo="r")
    mr = mergerequests.create_mr(
        task_id=task["id"], repo="r", source_branch=source, target_branch="main",
        bare_path=str(src), base_branch="main", source_path=str(src))
    # An MR that reached a merge button in the ordinary way has been reviewed:
    # mergerequests.merge_block_reason requires a recorded approval, so leaving
    # it unset here would be testing the override path, not this one.
    mergerequests.set_review_status(mr["id"], "approved")
    return mergerequests.get_mr(mr["id"]), src


def _resolver(monkeypatch, result="MERGED\n"):
    """Stand in for the model. Records every call so a test can prove the route
    did NOT reach for the model when it had no business doing so."""
    calls = []

    def fake(path, text):
        calls.append(path)
        assert "<<<<<<<" in text          # the route hands over real conflicted content
        return result

    monkeypatch.setattr(mergerequests, "ai_conflict_resolver", fake)
    return calls


class TestResolveRoute:
    def test_resolves_and_merges_naming_the_files(self, db_path, tmp_path, monkeypatch):
        mr, src = _mr(tmp_path)
        calls = _resolver(monkeypatch)
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert r.status_code == 200
        assert calls == ["x.py"]
        row = mergerequests.get_mr(mr["id"])
        assert row["status"] == "merged" and row["merge_sha"]
        assert row["merge_resolved"] == "x.py"          # recorded, not just flashed
        assert _git("show", "main:x.py", cwd=str(src)).stdout == "MERGED\n"
        assert "AI-resolved" in r.text and "x.py" in r.text

    def test_merged_view_keeps_saying_it_was_ai_resolved(self, db_path, tmp_path, monkeypatch):
        """The flash is gone on the next render — the badge must not be."""
        mr, src = _mr(tmp_path)
        _resolver(monkeypatch)
        TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        r = TestClient(app).get(f"/api/mr/{mr['id']}/view")
        assert r.status_code == 200
        assert "AI-resolved merge" in r.text and "x.py" in r.text

    def test_unresolvable_file_leaves_target_untouched(self, db_path, tmp_path, monkeypatch):
        mr, src = _mr(tmp_path)
        monkeypatch.setattr(mergerequests, "ai_conflict_resolver", lambda p, t: None)
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert r.status_code == 200
        assert mergerequests.get_mr(mr["id"])["status"] == "open"      # not merged
        assert _git("show", "main:x.py", cwd=str(src)).stdout == "AAA\n"
        assert _git("status", "--porcelain", cwd=str(src)).stdout.strip() == ""
        assert "AI resolution failed" in r.text and "untouched" in r.text
        assert "nothing was merged" in r.text
        assert "AI-resolved" not in r.text        # no success wording anywhere

    def test_refuses_when_the_branch_merges_cleanly(self, db_path, tmp_path, monkeypatch):
        """Never the default merge path: with nothing to resolve, the model is
        not asked and the merge does not happen through this route."""
        mr, src = _mr(tmp_path, source="C")
        calls = _resolver(monkeypatch)
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert r.status_code == 200
        assert calls == []
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        assert "merges cleanly" in r.text
        assert _git("show", "main:other.py", cwd=str(src)).returncode != 0

    def test_refuses_when_mr_is_not_open(self, db_path, tmp_path, monkeypatch):
        mr, src = _mr(tmp_path)
        calls = _resolver(monkeypatch)
        mergerequests.set_status(mr["id"], "closed")
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert r.status_code == 200 and calls == []
        assert "already closed" in r.text
        assert mergerequests.get_mr(mr["id"])["status"] == "closed"

    def test_review_gate_applies_and_force_overrides(self, db_path, tmp_path, monkeypatch):
        mr, src = _mr(tmp_path)
        calls = _resolver(monkeypatch)
        mergerequests.set_review_status(mr["id"], "changes_requested")
        c = TestClient(app)
        r = c.post(f"/api/mr/{mr['id']}/merge-resolve")
        assert calls == [] and "merge blocked" in r.text
        assert mergerequests.get_mr(mr["id"])["status"] == "open"
        r = c.post(f"/api/mr/{mr['id']}/merge-resolve", data={"force": "1"})
        assert calls == ["x.py"]
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"

    def test_missing_mr_is_404_not_500(self, db_path):
        assert TestClient(app).post("/api/mr/nope/merge-resolve").status_code == 404

    def test_self_merge_clears_the_self_update_cache(self, db_path, tmp_path, monkeypatch):
        """A self-merge through AI resolution must drop the cached behind-status
        exactly like the plain merge route does — record_merge_success owns
        'a merge happened' for every path. Without this, the banner and the
        Restart endpoint keep reporting 'up to date' for up to the 60s TTL."""
        from core import self_update
        mr, src = _mr(tmp_path)
        _resolver(monkeypatch)
        cleared = []
        monkeypatch.setattr(self_update, "is_daemon_checkout", lambda path: True)
        monkeypatch.setattr(self_update, "clear_cache", lambda: cleared.append(True))
        r = TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert r.status_code == 200
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"
        assert cleared == [True]

    def test_merge_into_another_repo_does_not_clear_self_update_cache(self, db_path, tmp_path, monkeypatch):
        from core import self_update
        mr, src = _mr(tmp_path)
        _resolver(monkeypatch)
        cleared = []
        monkeypatch.setattr(self_update, "is_daemon_checkout", lambda path: False)
        monkeypatch.setattr(self_update, "clear_cache", lambda: cleared.append(True))
        TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"
        assert cleared == []


class TestButtonVisibility:
    def test_offered_only_when_conflicting(self, db_path, tmp_path):
        conflicting, _ = _mr(tmp_path / "a")
        clean, _ = _mr(tmp_path / "b", source="C")
        c = TestClient(app)
        assert "merge-resolve" in c.get(f"/api/mr/{conflicting['id']}/view").text
        assert "merge-resolve" not in c.get(f"/api/mr/{clean['id']}/view").text

    def test_not_offered_once_merged(self, db_path, tmp_path, monkeypatch):
        mr, src = _mr(tmp_path)
        _resolver(monkeypatch)
        TestClient(app).post(f"/api/mr/{mr['id']}/merge-resolve")
        assert "merge-resolve" not in TestClient(app).get(f"/api/mr/{mr['id']}/view").text


def test_mark_merged_records_resolved_files(db_path):
    """A plain merge must not look auto-resolved — the column stays empty."""
    plain = mergerequests.create_mr(task_id="t", repo="r", source_branch="s",
                                    target_branch="main")
    mergerequests.mark_merged(plain["id"], "abc1234")
    assert not mergerequests.get_mr(plain["id"])["merge_resolved"]
    ai = mergerequests.create_mr(task_id="t", repo="r", source_branch="s2",
                                 target_branch="main")
    mergerequests.mark_merged(ai["id"], "def5678", ["a.py", "b/c.py"])
    assert mergerequests.get_mr(ai["id"])["merge_resolved"] == "a.py\nb/c.py"
