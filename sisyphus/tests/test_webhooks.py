"""Tests for forge webhook ingestion — parser (core/webhooks.py) and the
receiver endpoint (web/api.py) that maps events back to a task."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from web.app import app

from core import store, webhooks


def _client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _loopback_client():
    # These tests exercise event→task mapping with no webhook secret set;
    # TestClient's host is "testclient", so simulate a loopback caller.
    with patch("web.auth._is_local", return_value=True):
        yield


# ── Parser ──────────────────────────────────────────────────────────────


class TestGitHubParse:
    def test_pr_merged(self):
        p = {"action": "closed",
             "pull_request": {"merged": True, "html_url": "https://gh/pr/7"}}
        assert webhooks.parse("github", p) == ("https://gh/pr/7", "[external] GitHub PR merged")

    def test_pr_closed_unmerged_is_generic(self):
        p = {"action": "closed",
             "pull_request": {"merged": False, "html_url": "https://gh/pr/7"}}
        url, summary = webhooks.parse("github", p)
        assert url == "https://gh/pr/7"
        assert "closed" in summary

    def test_review_submitted(self):
        p = {"review": {"state": "changes_requested"},
             "pull_request": {"html_url": "https://gh/pr/7"}}
        assert webhooks.parse("github", p) == (
            "https://gh/pr/7", "[external] GitHub review: changes_requested")

    def test_ci_failure(self):
        p = {"workflow_run": {"conclusion": "failure", "name": "CI",
                              "pull_requests": [{"html_url": "https://gh/pr/7"}]}}
        url, summary = webhooks.parse("github", p)
        assert url == "https://gh/pr/7"
        assert "CI failed" in summary

    def test_ci_success_ignored(self):
        p = {"workflow_run": {"conclusion": "success",
                              "pull_requests": [{"html_url": "https://gh/pr/7"}]}}
        assert webhooks.parse("github", p) is None

    def test_unmappable_ignored(self):
        assert webhooks.parse("github", {"zen": "ping"}) is None


class TestGitLabParse:
    def test_mr_action(self):
        p = {"object_kind": "merge_request",
             "object_attributes": {"action": "merge", "url": "https://gl/mr/3"}}
        assert webhooks.parse("gitlab", p) == ("https://gl/mr/3", "[external] GitLab MR merge")

    def test_pipeline_failed(self):
        p = {"object_kind": "pipeline",
             "object_attributes": {"status": "failed"},
             "merge_request": {"url": "https://gl/mr/3"}}
        url, summary = webhooks.parse("gitlab", p)
        assert url == "https://gl/mr/3"
        assert "failed" in summary

    def test_note_comment(self):
        p = {"object_kind": "note",
             "merge_request": {"url": "https://gl/mr/3"}}
        url, summary = webhooks.parse("gitlab", p)
        assert url == "https://gl/mr/3"
        assert "comment" in summary

    def test_unmappable_ignored(self):
        assert webhooks.parse("gitlab", {"object_kind": "push"}) is None


def test_unknown_platform_ignored():
    assert webhooks.parse("bitbucket", {}) is None


# ── Endpoint ────────────────────────────────────────────────────────────


def _task_with_mr(url):
    t = store.create_task("ship it", repo="sisyphus")
    store.post_message(t["id"], "engineer", kind="mr", text=f"Add x: {url}")
    return t


def test_webhook_maps_to_task_and_emits(db_path):
    t = _task_with_mr("https://gh/pr/7")
    store.set_status(t["id"], "waiting")
    body = {"action": "closed",
            "pull_request": {"merged": True, "html_url": "https://gh/pr/7"}}
    with patch("core.orchestrator.emit") as mock_emit:
        r = _client().post("/api/v1/webhooks/github", json=body)
    assert r.status_code == 200
    assert r.json()["task_id"] == t["id"]
    mock_emit.assert_called_once_with(t["id"], "external")
    thread = store.get_thread(t["id"])
    assert thread[-1]["kind"] == "external"
    assert "merged" in thread[-1]["text"]
    # a waiting task is re-opened so the loop re-engages
    assert store.get_task(t["id"])["status"] == "active"


def test_webhook_reopens_done_task(db_path):
    t = _task_with_mr("https://gh/pr/9")
    store.set_status(t["id"], "done")
    body = {"workflow_run": {"conclusion": "failure", "name": "CI",
                             "pull_requests": [{"html_url": "https://gh/pr/9"}]}}
    with patch("core.orchestrator.emit"):
        r = _client().post("/api/v1/webhooks/github", json=body)
    assert r.json()["task_id"] == t["id"]
    assert store.get_task(t["id"])["status"] == "active"


def test_webhook_unmatched_url_is_noop(db_path):
    _task_with_mr("https://gh/pr/7")
    body = {"action": "closed",
            "pull_request": {"merged": True, "html_url": "https://gh/pr/OTHER"}}
    with patch("core.orchestrator.emit") as mock_emit:
        r = _client().post("/api/v1/webhooks/github", json=body)
    assert r.json() == {"ok": True, "matched": False}
    mock_emit.assert_not_called()


def test_webhook_unparseable_event_ignored(db_path):
    with patch("core.orchestrator.emit") as mock_emit:
        r = _client().post("/api/v1/webhooks/github", json={"zen": "ping"})
    assert r.json() == {"ok": True, "ignored": True}
    mock_emit.assert_not_called()
