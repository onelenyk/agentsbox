"""Tests for webhook authentication (core/webhooks.py and web/api.py)."""
import hmac
import hashlib
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import config
from web.app import app


@contextmanager
def _webhook_secret(value):
    """Enable webhook auth by putting the secret where the config placeholder
    resolves from — the vault (its per-process cache stands in for it) — then
    reload config. Always restores on exit."""
    from core import credentials
    prev = credentials._cache.get("SISYPHUS_WEBHOOK_SECRET")
    credentials._cache["SISYPHUS_WEBHOOK_SECRET"] = value
    config.reload_config()
    try:
        yield
    finally:
        credentials._cache["SISYPHUS_WEBHOOK_SECRET"] = prev
        config.reload_config()


def _client():
    """Return a TestClient (default host is 'testclient', not loopback)."""
    return TestClient(app)


class TestGitHubWebhookAuth:
    def test_valid_github_signature_accepted(self, db_path):
        """GitHub webhook with valid HMAC-SHA256 signature in X-Hub-Signature-256 header."""
        secret = "test-secret-key"
        with _webhook_secret(secret):
            body_bytes = b'{"zen": "test", "hook_id": 123}'
            expected_sig = "sha256=" + hmac.new(
                secret.encode(),
                body_bytes,
                hashlib.sha256
            ).hexdigest()

            r = _client().post(
                "/api/v1/webhooks/github",
                content=body_bytes,
                headers={"X-Hub-Signature-256": expected_sig},
            )
            assert r.status_code == 200
            assert r.json()["ok"] is True

    def test_invalid_github_signature_rejected(self, db_path):
        """GitHub webhook with wrong signature is rejected with 401."""
        secret = "test-secret-key"
        with _webhook_secret(secret):
            body_bytes = b'{"zen": "test"}'
            wrong_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

            r = _client().post(
                "/api/v1/webhooks/github",
                content=body_bytes,
                headers={"X-Hub-Signature-256": wrong_sig},
            )
            assert r.status_code == 401
            assert "invalid webhook signature" in r.json()["detail"]

    def test_missing_github_signature_rejected(self, db_path):
        """GitHub webhook without X-Hub-Signature-256 header is rejected."""
        with _webhook_secret("test-secret-key"):
            r = _client().post(
                "/api/v1/webhooks/github",
                json={"zen": "test"},
            )
            assert r.status_code == 401
            assert "invalid webhook signature" in r.json()["detail"]


class TestGitLabWebhookAuth:
    def test_valid_gitlab_token_accepted(self, db_path):
        """GitLab webhook with matching X-Gitlab-Token header is accepted."""
        secret = "test-gitlab-token"
        with _webhook_secret(secret):
            r = _client().post(
                "/api/v1/webhooks/gitlab",
                json={"object_kind": "merge_request"},
                headers={"X-Gitlab-Token": secret},
            )
            assert r.status_code == 200
            assert r.json()["ok"] is True

    def test_invalid_gitlab_token_rejected(self, db_path):
        """GitLab webhook with wrong token is rejected."""
        secret = "test-gitlab-token"
        with _webhook_secret(secret):
            r = _client().post(
                "/api/v1/webhooks/gitlab",
                json={"object_kind": "merge_request"},
                headers={"X-Gitlab-Token": "wrong-token"},
            )
            assert r.status_code == 401
            assert "invalid webhook token" in r.json()["detail"]

    def test_missing_gitlab_token_rejected(self, db_path):
        """GitLab webhook without X-Gitlab-Token header is rejected."""
        with _webhook_secret("test-gitlab-token"):
            r = _client().post(
                "/api/v1/webhooks/gitlab",
                json={"object_kind": "merge_request"},
            )
            assert r.status_code == 401
            assert "invalid webhook token" in r.json()["detail"]


class TestSisyphusTokenAuth:
    def test_valid_sisyphus_token_accepted(self, db_path):
        """Generic/Jira webhook with matching X-Sisyphus-Token header is accepted."""
        secret = "test-sisyphus-token"
        with _webhook_secret(secret):
            r = _client().post(
                "/api/v1/webhooks/jira",
                json={"event": "test"},
                headers={"X-Sisyphus-Token": secret},
            )
            assert r.status_code == 200
            assert r.json()["ok"] is True

    def test_invalid_sisyphus_token_rejected(self, db_path):
        """Generic/Jira webhook with wrong token is rejected."""
        secret = "test-sisyphus-token"
        with _webhook_secret(secret):
            r = _client().post(
                "/api/v1/webhooks/jira",
                json={"event": "test"},
                headers={"X-Sisyphus-Token": "wrong-token"},
            )
            assert r.status_code == 401
            assert "invalid webhook token" in r.json()["detail"]


class TestWebhookNoSecretConfigured:
    def test_loopback_allowed_without_secret(self, db_path):
        """When no secret is configured, loopback clients can still send webhooks."""
        with _webhook_secret(None):
            # Mock _is_local from web.auth to simulate loopback
            with patch("web.auth._is_local", return_value=True):
                r = _client().post(
                    "/api/v1/webhooks/github",
                    json={"zen": "test"},
                )
                assert r.status_code == 200
                assert r.json()["ok"] is True

    def test_non_loopback_rejected_without_secret(self, db_path):
        """When no secret is configured, non-loopback clients are rejected with 403."""
        with _webhook_secret(None):
            # Mock _is_local to return False to simulate a remote client
            with patch("web.auth._is_local", return_value=False):
                r = _client().post(
                    "/api/v1/webhooks/github",
                    json={"zen": "test"},
                )
                assert r.status_code == 403
                assert "webhook secret not configured" in r.json()["detail"]
