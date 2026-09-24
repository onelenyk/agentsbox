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
