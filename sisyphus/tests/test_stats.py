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
    assert "Agents at work" in v.text
    assert "Cost by agent" in v.text


def test_dashboard_data_shape(db_path):
    _seed(db_path)
    from core.stats import dashboard
    d = dashboard()
    assert "ov" in d and "active" in d and "waiting" in d and "recent" in d
    assert any(x["id"] for x in d["active"])   # the active seed task
