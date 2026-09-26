"""Tests for core.readiness assessment.

Covers the readiness check items: check gate, delivery, auto-merge, isolation,
egress, forge token, pilot mode. Also tests the API route and CLI command.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from core import readiness


class TestReadinessAssess:
    """Test core.readiness.assess() with various configurations."""

    def test_tracker_configured(self):
        """Tracker item passes when Jira is configured."""
        repo = {"name": "test", "check": "test"}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.jira.configured", return_value=True):
                result = readiness.assess("test")
                tracker_item = [i for i in result["items"] if i["key"] == "tracker"][0]
                assert tracker_item["ok"] is True
                assert tracker_item["severity"] == "warn"

    def test_tracker_not_configured(self):
        """Tracker item warns when Jira is not configured."""
        repo = {"name": "test", "check": "test"}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.jira.configured", return_value=False):
                result = readiness.assess("test")
                tracker_item = [i for i in result["items"] if i["key"] == "tracker"][0]
                assert tracker_item["ok"] is False
                assert tracker_item["severity"] == "warn"
                assert "fix" in tracker_item and tracker_item["fix"]

    def test_tracker_fix_names_integrations_not_env(self):
        """The tracker fix says where in the UI, never .env — credentials are
        entered only in Integrations → Connections now."""
        repo = {"name": "test", "check": "test"}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.jira.configured", return_value=False):
                result = readiness.assess("test")
                tracker_item = [i for i in result["items"] if i["key"] == "tracker"][0]
                assert "Integrations" in tracker_item["fix"]
                assert ".env" not in tracker_item["fix"]
                assert tracker_item["fix_href"] == "/integrations"
                assert tracker_item["text"] == "New Jira bugs won't become tasks on their own."

    def test_target_defaults_to_local_and_is_reported(self):
        """A repo without a target reads as local, and production is honoured."""
        repo = {"name": "test", "check": "test"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
        assert result["target"] == "local"

        repo["target"] = "production"
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
        assert result["target"] == "production"

    def test_missing_repo(self):
        """Assess returns block when repo is not found."""
        with patch("core.readiness.get_repo", return_value=None):
            result = readiness.assess("nonexistent")
            assert result["repo"] == "nonexistent"
            assert result["ready"] is False
            assert len(result["items"]) == 1
            assert result["items"][0]["key"] == "repo"
            assert result["items"][0]["ok"] is False
            assert result["items"][0]["severity"] == "block"

    def test_check_present(self):
        """Check item passes when check: is configured."""
        repo = {"name": "test", "check": "python -m pytest -q"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            check_item = [i for i in result["items"] if i["key"] == "check"][0]
            assert check_item["ok"] is True
            assert check_item["severity"] == "block"

    def test_check_missing(self):
        """Check item fails when check: is absent or empty."""
        repo = {"name": "test"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            check_item = [i for i in result["items"] if i["key"] == "check"][0]
            assert check_item["ok"] is False
            assert check_item["severity"] == "block"
            assert "fix" in check_item and check_item["fix"]

    def test_delivery_forge(self):
        """Delivery warns when using a forge (github/gitlab)."""
        for platform in ["github", "gitlab"]:
            repo = {"name": "test", "check": "test", "platform": platform}
            with patch("core.readiness.get_repo", return_value=repo):
                result = readiness.assess("test")
                delivery_item = [i for i in result["items"] if i["key"] == "delivery"][0]
                assert delivery_item["ok"] is True
                assert delivery_item["severity"] == "warn"

    def test_delivery_local(self):
        """Delivery warns when using local delivery."""
        repo = {"name": "test", "check": "test", "platform": "local"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            delivery_item = [i for i in result["items"] if i["key"] == "delivery"][0]
            assert delivery_item["ok"] is False
            assert delivery_item["severity"] == "warn"
            assert "fix" in delivery_item and delivery_item["fix"]

    def test_auto_merge_on_forge_blocks(self):
        """Auto-merge blocks when on and platform is a forge."""
        repo = {"name": "test", "check": "test", "platform": "github", "auto_merge": True}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            auto_merge_item = [i for i in result["items"] if i["key"] == "auto_merge"][0]
            assert auto_merge_item["ok"] is False
            assert auto_merge_item["severity"] == "block"

    def test_auto_merge_on_local_ok(self):
        """Auto-merge is OK when platform is local (even if on)."""
        repo = {"name": "test", "check": "test", "platform": "local", "auto_merge": True}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            auto_merge_item = [i for i in result["items"] if i["key"] == "auto_merge"][0]
            assert auto_merge_item["ok"] is True

    def test_auto_merge_off_ok(self):
        """Auto-merge is OK when off."""
        repo = {"name": "test", "check": "test", "platform": "github", "auto_merge": False}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            auto_merge_item = [i for i in result["items"] if i["key"] == "auto_merge"][0]
            assert auto_merge_item["ok"] is True

    def test_read_isolation_on(self):
        """Read isolation passes when sandbox.read_isolation is true."""
        repo = {"name": "test", "check": "test"}
        cfg_map = {"sandbox": {"read_isolation": True}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "read_isolation"][0]
                assert item["ok"] is True

    def test_read_isolation_off(self):
        """Read isolation blocks when sandbox.read_isolation is false or missing."""
        repo = {"name": "test", "check": "test"}
        cfg_map = {"sandbox": {"read_isolation": False}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "read_isolation"][0]
                assert item["ok"] is False
                assert item["severity"] == "block"

    def test_egress_on(self):
        """Egress passes when sandbox.egress is true."""
        repo = {"name": "test", "check": "test"}
        cfg_map = {"sandbox": {"egress": True}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "egress"][0]
                assert item["ok"] is True

    def test_egress_off(self):
        """Egress blocks when sandbox.egress is false or missing."""
        repo = {"name": "test", "check": "test"}
        cfg_map = {"sandbox": {"egress": False}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "egress"][0]
                assert item["ok"] is False
                assert item["severity"] == "block"

    def test_forge_token_github_configured(self):
        """Forge token passes when github token is configured."""
        repo = {"name": "test", "check": "test", "platform": "github"}
        cfg_map = {"github": {"token": "gh-token-123"}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                with patch("core.readiness._token", return_value="gh-token-123"):
                    result = readiness.assess("test")
                    item = [i for i in result["items"] if i["key"] == "forge_token"][0]
                    assert item["ok"] is True

    def test_forge_token_github_missing(self):
        """Forge token blocks when github token is missing."""
        repo = {"name": "test", "check": "test", "platform": "github"}
        cfg_map = {"github": {}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                with patch("core.readiness._token", return_value=None):
                    result = readiness.assess("test")
                    item = [i for i in result["items"] if i["key"] == "forge_token"][0]
                    assert item["ok"] is False
                    assert item["severity"] == "block"

    def test_forge_token_per_repo(self):
        """Forge token uses per-repo token if configured."""
        repo = {"name": "test", "check": "test", "platform": "github", "token": "per-repo-token"}
        cfg_map = {"github": {}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "forge_token"][0]
                assert item["ok"] is True

    def test_forge_token_local_not_needed(self):
        """Forge token is OK for local platform (no token needed)."""
        repo = {"name": "test", "check": "test", "platform": "local"}
        cfg_map = {"local": {}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                item = [i for i in result["items"] if i["key"] == "forge_token"][0]
                assert item["ok"] is True

    def test_pilot_mode_read_only(self):
        """Pilot mode passes when mode is read-only."""
        repo = {"name": "test", "check": "test", "mode": "read-only"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            item = [i for i in result["items"] if i["key"] == "pilot_mode"][0]
            assert item["ok"] is True
            assert item["severity"] == "warn"

    def test_pilot_mode_write(self):
        """Pilot mode warns when mode is write (the default)."""
        repo = {"name": "test", "check": "test", "mode": "write"}
        with patch("core.readiness.get_repo", return_value=repo):
            result = readiness.assess("test")
            item = [i for i in result["items"] if i["key"] == "pilot_mode"][0]
            assert item["ok"] is False
            assert item["severity"] == "warn"

    def test_ready_when_no_blocks_fail(self):
        """ready is true when no block items fail."""
        repo = {
            "name": "test",
            "check": "python -m pytest",
            "platform": "github",
            "auto_merge": False,
        }
        cfg_map = {
            "sandbox": {"read_isolation": True, "egress": True},
            "github": {"token": "gh-token"},
        }
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                with patch("core.readiness._token", return_value="gh-token"):
                    result = readiness.assess("test")
                    assert result["ready"] is True

    def test_not_ready_when_block_fails(self):
        """ready is false when any block item fails."""
        repo = {"name": "test"}  # missing check
        cfg_map = {"sandbox": {"read_isolation": True, "egress": True}}
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = readiness.assess("test")
                assert result["ready"] is False


class TestReadinessRoute:
    """Test the GET /api/repos/{name}/readiness route."""

    def test_readiness_route_404_when_repo_not_found(self):
        """Route returns 200 with error when repo not found."""
        from web.app import app
        client = TestClient(app)

        with patch("core.repos.load_repos", return_value=[]):
            response = client.get("/api/repos/nonexistent/readiness")
            # Route will render the template with a failed assessment
            assert response.status_code == 200
            # Template will show "Not ready" status
            assert ("not ready" in response.text.lower() or
                    "not found" in response.text.lower())

    def test_readiness_route_local_note(self):
        """A local-target repo's page notes that production checks are not applied."""
        from web.app import app
        client = TestClient(app)

        repo = {"name": "test", "check": "test"}
        with patch("core.repos.load_repos", return_value=[repo]), \
             patch("core.jira.configured", return_value=False):
            response = client.get("/api/repos/test/readiness")
        assert response.status_code == 200
        assert "production checks are not applied" in response.text

    def test_readiness_page_route_renders_full_page(self):
        """The full /repos/{name}/readiness page renders the assessment."""
        from web.app import app
        client = TestClient(app)

        repo = {"name": "test", "check": "test", "target": "production"}
        with patch("core.repos.load_repos", return_value=[repo]), \
             patch("core.jira.configured", return_value=False):
            response = client.get("/repos/test/readiness")
        assert response.status_code == 200
        assert "Production readiness" in response.text
        assert "New Jira bugs won" in response.text
        assert "become tasks on their own" in response.text
        assert 'href="/integrations"' in response.text


class TestReadinessCLI:
    """Test the repos readiness CLI command."""

    def test_cli_readiness_ready(self, capsys):
        """CLI exits 0 when ready."""
        from sisyphus import cli
        from click.testing import CliRunner

        repo = {
            "name": "test",
            "check": "pytest",
            "platform": "github",
            "auto_merge": False,
        }
        cfg_map = {
            "sandbox": {"read_isolation": True, "egress": True},
            "github": {"token": "token"},
        }

        runner = CliRunner()
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                with patch("core.readiness._token", return_value="token"):
                    result = runner.invoke(cli, ["repos", "readiness", "test"])
                    assert result.exit_code == 0
                    assert "✓" in result.output
                    assert "check" in result.output

    def test_cli_readiness_not_ready(self):
        """CLI exits 1 when a production repo is not ready."""
        from sisyphus import cli
        from click.testing import CliRunner

        repo = {"name": "test", "target": "production"}  # missing check
        cfg_map = {"sandbox": {"read_isolation": False, "egress": False}}

        runner = CliRunner()
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = runner.invoke(cli, ["repos", "readiness", "test"])
                assert result.exit_code == 1
                assert "✗" in result.output

    def test_cli_local_repo_exits_zero_even_when_not_ready(self):
        """A local repo never fails the CLI — production checks are not applied."""
        from sisyphus import cli
        from click.testing import CliRunner

        repo = {"name": "test"}  # missing check, target defaults to local
        cfg_map = {"sandbox": {"read_isolation": False, "egress": False}}

        runner = CliRunner()
        with patch("core.readiness.get_repo", return_value=repo):
            with patch("core.readiness.get_config") as mock_cfg:
                def _cfg(key, default=None):
                    return cfg_map.get(key, default)
                mock_cfg.side_effect = _cfg
                result = runner.invoke(cli, ["repos", "readiness", "test"])
                assert result.exit_code == 0
                assert "production checks are not applied" in result.output
