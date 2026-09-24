"""core.jira and core.integrations — every call leaves one journal row, and
the credentials leave no trace in it."""
import json
import urllib.error
from io import BytesIO

import pytest

from core import integrations, jira


@pytest.fixture
def jira_config(monkeypatch):
    values = {"jira.base_url": "https://x.atlassian.net", "jira.email": "me@x.test",
              "jira.token": "SECRET-TOKEN"}
    monkeypatch.setattr("core.config.get_config", lambda key=None, default=None: values.get(key, default))
    return values


class _Resp:
    def __init__(self, obj):
        self.raw = json.dumps(obj).encode()
    def read(self):
        return self.raw
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _fake_urlopen(monkeypatch, handler):
    seen = []
    def urlopen(req, timeout=0):
        seen.append(req)
        return _Resp(handler(req))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return seen


def test_unconfigured_is_a_clear_error_not_a_request(monkeypatch, db_path):
    monkeypatch.setattr("core.config.get_config", lambda key=None, default=None: "${JIRA_API_TOKEN}")
    assert not jira.configured()
    with pytest.raises(jira.JiraError, match="not configured"):
        jira.myself()


def test_search_pages_and_journals(monkeypatch, db_path, jira_config):
    pages = [{"issues": [{"key": "KMP-1", "id": "1", "fields": {"summary": "Crash on start",
                "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Steps"}]}]},
                "issuetype": {"name": "Bug"}, "priority": {"name": "High"}, "status": {"name": "To Do"},
                "labels": ["ios"], "components": [{"name": "app"}], "reporter": {"displayName": "Q"}}}],
              "nextPageToken": "t2"},
             {"issues": [{"key": "KMP-2", "id": "2", "fields": {"summary": "Second"}}], "isLast": True}]
    seen = _fake_urlopen(monkeypatch, lambda req: pages.pop(0))
    out = jira.search("project = KMP AND type = Bug", actor="daemon:jira-poll")
    assert [i["key"] for i in out] == ["KMP-1", "KMP-2"]
    assert out[0]["fields"]["description"] == "Steps"
    assert out[0]["fields"]["priority"] == "High" and out[0]["url"].endswith("/browse/KMP-1")
    assert "nextPageToken=t2" in seen[1].full_url
    ev = integrations.list_events("jira")[0]
    assert (ev["action"], ev["actor"], ev["count"], ev["ok"]) == ("search", "daemon:jira-poll", 2, 1)
    assert ev["subject"] == "project = KMP AND type = Bug"
    assert "SECRET-TOKEN" not in json.dumps(ev)


def test_transition_picks_by_target_status_and_journals(monkeypatch, db_path, jira_config):
    calls = []
    def handler(req):
        calls.append((req.get_method(), req.full_url, req.data))
        if req.full_url.endswith("/transitions") and req.get_method() == "GET":
            return {"transitions": [{"id": "31", "name": "Start", "to": {"name": "In Progress"}}]}
        return {}
    _fake_urlopen(monkeypatch, handler)
    res = jira.transition("KMP-1", "in progress", actor="agent:engineer@task-1", task_id="task-1")
    assert res["status"] == "In Progress"
    assert json.loads(calls[-1][2]) == {"transition": {"id": "31"}}
    ev = integrations.list_events("jira")[0]
    assert ev["action"] == "transition" and ev["detail"] == "moved KMP-1 to In Progress"
    assert ev["task_id"] == "task-1" and ev["meta"]["to"] == "In Progress"


def test_a_failure_is_journaled_with_jiras_words_and_no_secret(monkeypatch, db_path, jira_config):
    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b'{"errorMessages":["Basic auth failed"]}'))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(jira.JiraError, match="401") as exc:
        jira.myself(actor="human:ui")
    assert "SECRET-TOKEN" not in str(exc.value)
    ev = integrations.list_events("jira", only_errors=True)[0]
    assert ev["ok"] == 0 and "Basic auth failed" in ev["error"]
    s = integrations.summary("jira")
    assert s["calls"] == 1 and s["failed"] == 1


def test_adf_to_text_flattens_paragraphs_and_lists():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "One"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "b"}]}]}]}]}
    assert jira.adf_to_text(doc).split() == ["One", "a", "b"]
