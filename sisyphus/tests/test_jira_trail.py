"""Tests for Jira task trail — origin display, filtering, and detail view."""
from core import store


class TestJiraKey:
    """core.store.jira_key() helper for extracting Jira key from origin."""

    def test_jira_key_extracts_key(self):
        """Extract key from 'jira:KEY' origin."""
        task = {"origin": "jira:KMP-12"}
        assert store.jira_key(task) == "KMP-12"

    def test_jira_key_handles_various_formats(self):
        """Extract key from various Jira key formats."""
        for key in ["ABC-1", "PROJ-999", "XYZ-000"]:
            task = {"origin": f"jira:{key}"}
            assert store.jira_key(task) == key

    def test_jira_key_returns_none_for_non_jira(self):
        """Return None when origin is not Jira."""
        test_cases = [
            {"origin": "autopilot:id123"},
            {"origin": None},
            {"origin": ""},
            {},
        ]
        for task in test_cases:
            assert store.jira_key(task) is None


class TestTheBoardShowsWhereATaskCameFrom:
    def _client(self):
        from fastapi.testclient import TestClient
        from web.app import app
        return TestClient(app)

    def test_a_jira_task_gets_a_linked_tag_and_the_filter_value(self, db_path, monkeypatch):
        from core import store
        monkeypatch.setattr("core.jira._settings", lambda: {"base_url": "https://x.atlassian.net", "email": "", "token": ""})
        t = store.create_task("KMP-12 · Crash on start\nJira: https://x.atlassian.net/browse/KMP-12 · Bug · Priority: High · Status: To Do",
                              repo="test-repo", title="KMP-12 · Crash on start", origin="jira:KMP-12")
        html = self._client().get("/api/tasks/list").text
        row = html.split(t["id"], 1)[1].split("</li>", 1)[0]
        assert 'data-origin="jira"' in html
        assert "tag--jira" in row and "KMP-12" in row
        assert 'href="https://x.atlassian.net/browse/KMP-12"' in row
        assert 'value="jira"' in html            # the Asked-by filter offers it

    def test_without_a_base_url_the_tag_has_no_link(self, db_path, monkeypatch):
        from core import store
        monkeypatch.setattr("core.jira._settings", lambda: {"base_url": "", "email": "", "token": ""})
        t = store.create_task("KMP-1 · x", repo="test-repo", title="KMP-1 · x", origin="jira:KMP-1")
        row = self._client().get("/api/tasks/list").text.split(t["id"], 1)[1].split("</li>", 1)[0]
        assert "tag--jira" in row and "/browse/" not in row

    def test_the_task_page_says_from_jira_with_type_and_priority(self, db_path, monkeypatch):
        from core import store
        monkeypatch.setattr("core.jira._settings", lambda: {"base_url": "https://x.atlassian.net", "email": "", "token": ""})
        t = store.create_task("KMP-12 · Crash on start\nJira: https://x.atlassian.net/browse/KMP-12 · Bug · Priority: High · Status: To Do",
                              repo="test-repo", title="KMP-12 · Crash on start", origin="jira:KMP-12")
        html = self._client().get(f"/api/tasks/{t['id']}/detail").text
        assert "From Jira" in html and "KMP-12" in html and "Bug" in html and "High" in html
