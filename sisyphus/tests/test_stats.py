"""Tests for core/stats.py + the /stats page."""
from fastapi.testclient import TestClient
from web.app import app

from core import store, usage
from core.stats import overview


def _seed(db_path):
    usage.init_usage_table()
    d = store.create_task("done one", repo="sisyphus"); store.set_status(d["id"], "done")
    store.post_message(d["id"], "system", kind="worker_started", text="spawning engineer")
    store.post_message(d["id"], "engineer", kind="commit", text="did it (abc12345)")
    store.post_message(d["id"], "reviewer", kind="result", text="APPROVED: ok")
    store.create_task("active one", repo="ios-app")
    f = store.create_task("failed one"); store.set_status(f["id"], "failed")
    usage.record_run(d["id"], "engineer", "deepseek",
                     usage.parse_usage('{"part":{"type":"step-finish","tokens":{"input":100,"output":20,"reasoning":5,"cache":{"read":0,"write":0}},"cost":0.01}}'), 1500)
    return d


def test_overview_shape(db_path):
    _seed(db_path)
    s = overview()
    assert s["tasks"]["total"] == 3
    assert s["tasks"]["done"] == 1 and s["tasks"]["failed"] == 1
    assert s["tasks"]["completion_rate"] == 50   # 1 done of 2 terminal
    assert s["activity"]["commits"] == 1
    assert s["activity"]["approvals"] == 1
    assert s["activity"]["worker_runs"] == 1
    assert s["usage"]["totals"]["runs"] == 1
    assert round(s["usage"]["totals"]["cost"], 3) == 0.01
    assert any(r["agent"] == "engineer" for r in s["usage"]["by_agent"])


def test_stats_api(db_path):
    _seed(db_path)
    r = TestClient(app).get("/api/v1/stats")
    assert r.status_code == 200
    assert r.json()["tasks"]["total"] == 3


def test_stats_page_and_view(db_path):
    _seed(db_path)
    c = TestClient(app)
    assert c.get("/stats").status_code == 200
    view = c.get("/api/stats/view")
    assert view.status_code == 200
    assert "Total cost" in view.text
    assert "Cost by agent" in view.text
    assert "engineer" in view.text


def test_dashboard_page_and_view(db_path):
    _seed(db_path)
    c = TestClient(app)
    assert c.get("/dashboard").status_code == 200
    v = c.get("/api/dashboard/view")
    assert v.status_code == 200
    assert "Lately" in v.text
    assert "Spend by agent" not in v.text      # that chart lives on Statistics


def test_dashboard_data_shape(db_path):
    _seed(db_path)
    from core.stats import dashboard
    d = dashboard()
    assert "ov" in d and "active" in d and "waiting" in d and "recent" in d
    assert any(x["id"] for x in d["active"])   # the active seed task


# ── Readiness on Home ──────────────────────────────────────────────────────


def _assess_map(results):
    def _assess(name):
        if name in results:
            return results[name]
        raise RuntimeError(f"unexpected repo {name}")
    return _assess


def _visible_text(html):
    """The rendered text with tags stripped — URLs and attributes don't count."""
    import re
    return re.sub(r"<[^>]+>", " ", html)


def test_dashboard_local_repo_shows_calm_line_no_counts(db_path):
    """A local-target repo reads one calm line on Home and no red counts."""
    from unittest.mock import patch
    assess = _assess_map({
        "alpha": {"repo": "alpha", "target": "local", "ready": False, "items": [
            {"key": "check", "ok": False, "severity": "block", "text": "no check", "fix": "add one", "fix_href": ""},
            {"key": "delivery", "ok": False, "severity": "warn", "text": "local only", "fix": "", "fix_href": ""}]},
    })
    repos = [{"name": "alpha", "source": "/tmp/alpha", "branch": "main"}]
    with patch("core.repos.load_repos", return_value=repos), \
         patch("core.readiness.assess", side_effect=assess):
        html = TestClient(app).get("/api/dashboard/view").text
    assert "local development repo — production checks not applied" in html
    assert _visible_text(html).count("alpha") == 1
    assert "blocking" not in html
    assert "blockers" not in html
    assert "warnings" not in html
    assert "Is this repo safe to hand to agents unattended?" in html
    assert 'href="/repos/alpha/readiness"' in html


def test_dashboard_production_repo_lists_failing_items_blocking_first(db_path):
    """A production-target repo lists its failing items, blocking first."""
    from unittest.mock import patch
    assess = _assess_map({
        "beta": {"repo": "beta", "target": "production", "ready": False, "items": [
            {"key": "delivery", "ok": False, "severity": "warn", "text": "Merges happen locally", "fix": "Open the Edit page", "fix_href": "/repos/beta"},
            {"key": "check", "ok": False, "severity": "block", "text": "Nothing verifies a merge request", "fix": "add one", "fix_href": ""}]},
    })
    repos = [{"name": "beta", "source": "/tmp/beta", "branch": "main"}]
    with patch("core.repos.load_repos", return_value=repos), \
         patch("core.readiness.assess", side_effect=assess):
        html = TestClient(app).get("/api/dashboard/view").text
    # blocking item renders first, marked as blocking
    assert html.index("Nothing verifies a merge request") < html.index("Merges happen locally")
    assert "blocking" in html
    assert "Nothing verifies a merge request" in html
    assert "Merges happen locally" in html
    # fix links where a UI place exists
    assert 'href="/repos/beta"' in html
    assert 'href="/repos/beta/readiness"' in html
    assert "1 blocking · 1 warning" in html
    assert _visible_text(html).count("beta") == 1


def test_dashboard_shows_ready_for_a_ready_production_repo(db_path):
    """A fully-ok production repo reads 'ready for a pilot' and hides no counts."""
    from unittest.mock import patch
    assess = _assess_map({"alpha": {"repo": "alpha", "target": "production", "ready": True, "items": [
        {"key": "check", "ok": True, "severity": "block", "text": "ok", "fix": "", "fix_href": ""}]}})
    with patch("core.repos.load_repos", return_value=[{"name": "alpha", "source": "/tmp/a", "branch": "main"}]), \
         patch("core.readiness.assess", side_effect=assess):
        html = TestClient(app).get("/api/dashboard/view").text
    assert "ready for a pilot" in html
    assert "blocking" not in html


def test_dashboard_survives_an_assess_that_raises(db_path):
    """One failing assess marks that repo 'could not assess' and keeps the rest."""
    from unittest.mock import patch
    def _assess(name):
        if name == "beta":
            raise RuntimeError("boom")
        return {"repo": name, "target": "production", "ready": True, "items": [
            {"key": "check", "ok": True, "severity": "block", "text": "ok", "fix": "", "fix_href": ""}]}
    repos = [{"name": n, "source": f"/tmp/{n}", "branch": "main"} for n in ("alpha", "beta")]
    with patch("core.repos.load_repos", return_value=repos), \
         patch("core.readiness.assess", side_effect=_assess):
        resp = TestClient(app).get("/api/dashboard/view")
    assert resp.status_code == 200
    assert "could not assess" in resp.text
    assert "ready for a pilot" in resp.text      # the healthy repo still renders
    assert "Lately" in resp.text
