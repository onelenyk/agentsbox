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


class TestRetryLogic:
    """Test retry logic for transient errors (429, 5xx)."""

    def test_429_with_retry_after_is_retried_and_succeeds(self, monkeypatch, db_path, jira_config):
        """A 429 with Retry-After sleeps and retries, succeeds, and journals retries=1."""
        import time as time_module

        call_count = [0]
        sleep_calls = []

        def handler(req):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: return 429 with Retry-After header
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                    {"Retry-After": "0.01"}, BytesIO(b'{"errorMessages":["rate limit"]}'))
            # Second call: succeed
            return {"key": "KMP-1", "id": "1", "fields": {"summary": "Test"}}

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        _fake_urlopen(monkeypatch, handler)
        monkeypatch.setattr("time.sleep", mock_sleep)

        out = jira.get_issue("KMP-1", actor="agent:test@task-1", task_id="task-1")
        assert out["key"] == "KMP-1"
        assert call_count[0] == 2  # Called twice
        assert len(sleep_calls) == 1  # Slept once
        assert sleep_calls[0] == 0.01  # Honored Retry-After

        ev = integrations.list_events("jira")[0]
        assert ev["ok"] == 1
        assert ev["meta"].get("retries") == 1  # Journaled

    def test_429_with_retry_after_capped_at_30s(self, monkeypatch, db_path, jira_config):
        """Retry-After headers > 30s are capped at 30s."""
        sleep_calls = []
        call_count = [0]

        def handler(req):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                    {"Retry-After": "999"}, BytesIO(b'{"errorMessages":["rate limit"]}'))
            return {"key": "KMP-1", "id": "1", "fields": {"summary": "Test"}}

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        _fake_urlopen(monkeypatch, handler)
        monkeypatch.setattr("time.sleep", mock_sleep)

        jira.get_issue("KMP-1", actor="agent:test@task-1")
        assert sleep_calls[0] <= 30  # Capped
        assert call_count[0] == 2

    def test_400_is_not_retried(self, monkeypatch, db_path, jira_config):
        """A 400 (Bad Request) is not retried; raised immediately."""
        call_count = [0]

        def handler(req):
            call_count[0] += 1
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request",
                {}, BytesIO(b'{"errorMessages":["invalid JQL"]}'))

        _fake_urlopen(monkeypatch, handler)

        with pytest.raises(jira.JiraError, match="400"):
            jira.search("invalid jql", actor="test")

        assert call_count[0] == 1  # Called only once; no retry

    def test_three_503s_end_in_error(self, monkeypatch, db_path, jira_config):
        """Three 503s (exhausted retries) raises JiraError."""
        call_count = [0]

        def handler(req):
            call_count[0] += 1
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable",
                {}, BytesIO(b'{"errorMessages":["service unavailable"]}'))

        def mock_sleep(seconds):
            pass  # Don't actually sleep in tests

        _fake_urlopen(monkeypatch, handler)
        monkeypatch.setattr("time.sleep", mock_sleep)

        with pytest.raises(jira.JiraError, match="503"):
            jira.get_issue("KMP-1", actor="test")

        # Called on initial attempt + 3 retries = 4 times
        assert call_count[0] == 4


class TestCheckMethod:
    """Test the check() method for connection verification."""

    def test_check_ok_when_configured_and_reachable(self, monkeypatch, db_path, jira_config):
        """check() returns ok=True when configured and myself() succeeds."""
        def handler(req):
            return {"accountId": "123", "displayName": "Test User", "emailAddress": "test@example.com"}

        _fake_urlopen(monkeypatch, handler)

        result = jira.check()
        assert result["ok"] is True
        assert "Test User" in result["detail"] or "connected" in result["detail"].lower()

    def test_check_not_ok_when_not_configured(self, monkeypatch, db_path):
        """check() returns ok=False when Jira is not configured."""
        monkeypatch.setattr("core.config.get_config", lambda key=None, default=None: "${JIRA_API_TOKEN}")

        result = jira.check()
        assert result["ok"] is False
        assert "not configured" in result["detail"].lower()

    def test_check_not_ok_when_unreachable(self, monkeypatch, db_path, jira_config):
        """check() returns ok=False when Jira is unreachable."""
        def urlopen(req, timeout=0):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", urlopen)

        result = jira.check()
        assert result["ok"] is False
        assert "failed" in result["detail"].lower() or "refused" in result["detail"].lower()


class TestCLI:
    """Test the CLI command `jira check`."""

    def test_jira_check_cli_success(self, monkeypatch, db_path, jira_config):
        """CLI `jira check` exits 0 and prints connected message on success."""
        from click.testing import CliRunner
        from sisyphus import cli

        def handler(req):
            return {"accountId": "123", "displayName": "Alice", "emailAddress": "alice@example.com"}

        _fake_urlopen(monkeypatch, handler)

        runner = CliRunner()
        result = runner.invoke(cli, ["jira", "check"])
        assert result.exit_code == 0
        assert "✓" in result.output or "connected" in result.output.lower()

    def test_jira_check_cli_failure(self, monkeypatch, db_path):
        """CLI `jira check` exits 1 and prints error on failure."""
        from click.testing import CliRunner
        from sisyphus import cli

        monkeypatch.setattr("core.config.get_config", lambda key=None, default=None: "${UNCONFIGURED}")

        runner = CliRunner()
        result = runner.invoke(cli, ["jira", "check"])
        assert result.exit_code == 1
        assert "✗" in result.output or "not configured" in result.output.lower()
