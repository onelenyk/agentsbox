"""Browser-notification poll endpoint (/api/notifications/poll)."""
import asyncio

from core import store
from web.routes import notifications_poll


def _poll():
    return asyncio.run(notifications_poll())


def test_returns_waiting_and_terminal_not_active(db_path):
    a = store.create_task("active one", status="active")
    w = store.create_task("waiting one", status="waiting")
    d = store.create_task("done one", status="done")
    f = store.create_task("failed one", status="failed")

    items = _poll()["items"]
    ids = {i["task_id"] for i in items}
    assert w["id"] in ids and d["id"] in ids and f["id"] in ids
    assert a["id"] not in ids  # active tasks aren't notable
    # each item carries a stable dedupe key and the goal for the body
    by_id = {i["task_id"]: i for i in items}
    assert by_id[w["id"]]["status"] == "waiting"
    assert by_id[w["id"]]["goal"] == "waiting one"
    assert by_id[w["id"]]["key"].startswith(w["id"] + ":waiting:")


def test_excludes_stale_updates(db_path):
    from core.db import get_conn

    t = store.create_task("old done", status="done")
    conn = get_conn()
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                 ("2000-01-01T00:00:00+00:00", t["id"]))
    conn.commit()
    conn.close()

    ids = {i["task_id"] for i in _poll()["items"]}
    assert t["id"] not in ids  # older than the 15-minute window
