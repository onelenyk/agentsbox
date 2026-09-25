"""Tests for the /api/v1 shared-token guard (web/auth.py)."""
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import config
from web.app import app
from web import auth


@contextmanager
def _api_token(value):
    """Enable API auth by putting the token where the config placeholder
    resolves from — the vault (its per-process cache stands in for it) — then
    reload the cached config. Always restores on exit."""
    from core import credentials
    prev = credentials._cache.get("SISYPHUS_API_TOKEN")
    credentials._cache["SISYPHUS_API_TOKEN"] = value
    config.reload_config()
    try:
        yield
    finally:
        credentials._cache["SISYPHUS_API_TOKEN"] = prev
        config.reload_config()


def _client():
    # TestClient's default client host is "testclient" (not loopback), so the
    # localhost exemption doesn't apply — this exercises the enforced path.
    return TestClient(app)


def test_disabled_by_default(db_path):
    """Unresolved ${SISYPHUS_API_TOKEN} placeholder → auth off, API open."""
    with _api_token(None):
        assert auth.configured_token() is None
        assert _client().get("/api/v1/repos").status_code == 200


def test_enabled_rejects_missing_token(db_path):
    with _api_token("s3cret"):
        assert _client().get("/api/v1/repos").status_code == 401


def test_enabled_accepts_correct_token(db_path):
    with _api_token("s3cret"):
        r = _client().get("/api/v1/repos", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200


def test_enabled_rejects_wrong_token(db_path):
    with _api_token("s3cret"):
        r = _client().get("/api/v1/repos", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


def test_health_is_always_open(db_path):
    with _api_token("s3cret"):
        assert _client().get("/api/v1/health").status_code == 200


def test_webhooks_are_exempt(db_path):
    """Forges can't send our token — the webhook path stays open (it maps by
    MR URL and simply ignores anything it can't place). When no webhook secret
    is configured, loopback clients can still send."""
    with _api_token("s3cret"):
        # Mock _is_local to simulate loopback, allowing webhook without secret
        with patch("web.auth._is_local", return_value=True):
            r = _client().post("/api/v1/webhooks/github", json={"zen": "ping"})
            assert r.status_code == 200
            assert r.json() == {"ok": True, "ignored": True}


def test_static_assets_are_exempt(db_path):
    """The stylesheet is not a secret and a browser fetches it without headers."""
    with _api_token("s3cret"):
        assert _client().get("/static/app.css").status_code != 401


class TestTheGuardCoversTheWholeSurface:
    """The guard used to match only /api/v1, which left the half of the surface
    that actually does the damage open: the htmx UI is served from /api/* (no
    /v1) and carries the mutating routes. These pin the wider contract."""

    def test_ui_pages_are_gated(self, db_path):
        with _api_token("s3cret"):
            assert _client().get("/tasks").status_code == 401

    def test_mutating_ui_route_is_gated(self, db_path):
        """The one that matters: deleting a repo answered to anyone."""
        with _api_token("s3cret"):
            assert _client().post("/api/repos/whatever/delete").status_code == 401

    def test_agent_mcp_bridge_is_gated(self, db_path):
        with _api_token("s3cret"):
            assert _client().post("/mcp/agent", json={}).status_code == 401

    def test_correct_token_still_reaches_the_ui(self, db_path):
        with _api_token("s3cret"):
            r = _client().get("/tasks", headers={"Authorization": "Bearer s3cret"})
            assert r.status_code != 401

    def test_everything_open_when_no_token_configured(self, db_path):
        with _api_token(None):
            assert _client().get("/tasks").status_code != 401


class TestLocalhostExemption:
    class _FakeReq:
        def __init__(self, host, headers=None):
            self.client = type("C", (), {"host": host})()
            self.headers = headers or {}

    def test_loopback_is_local(self):
        assert auth._is_local(self._FakeReq("127.0.0.1"))
        assert auth._is_local(self._FakeReq("::1"))

    def test_remote_is_not_local(self):
        assert not auth._is_local(self._FakeReq("10.0.0.4"))
        assert not auth._is_local(self._FakeReq("testclient"))

    def test_a_proxy_cannot_borrow_the_exemption(self):
        """Behind a reverse proxy, request.client.host is the proxy — very often
        127.0.0.1. Trusting it there would hand the localhost exemption to the
        whole internet, so a forwarding header disqualifies the request."""
        for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
            assert not auth._is_local(
                self._FakeReq("127.0.0.1", {header: "203.0.113.9"})), header


class TestCrossSiteWritesAreRefused:
    """A website the user has open must not be able to POST to the daemon.

    The CSRF guard (web.auth.csrf_guard) refuses cross-site writes for the
    whole HTTP surface; only the credential routes used to (_from_this_page).
    TestClient's default host is "testserver", so "http://testserver" is the
    same-origin value to assert against."""

    def test_cross_site_origin_post_is_refused(self, db_path):
        r = _client().post("/api/v1/tasks", json={"goal": "nope"},
                           headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json()["detail"] == "cross-site request refused"

    def test_cross_site_sec_fetch_site_post_is_refused(self, db_path):
        r = _client().post("/api/v1/tasks", json={"goal": "nope"},
                           headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert r.json()["detail"] == "cross-site request refused"

    def test_same_origin_post_is_allowed(self, db_path):
        r = _client().post("/api/v1/tasks", json={"goal": "same-origin ok"},
                           headers={"Origin": "http://testserver"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_no_site_headers_post_is_allowed(self, db_path):
        """curl, the MCP server, workers and TestClient send neither header —
        they must pass unchanged."""
        r = _client().post("/api/v1/tasks", json={"goal": "no headers ok"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_cross_site_get_is_allowed(self, db_path):
        """Reads are never blocked — a CSRF guard only refuses writes."""
        r = _client().get("/api/v1/tasks", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    def test_webhook_path_cross_site_is_not_blocked_by_this_guard(self, db_path):
        """Forges post to the webhook path cross-origin and authenticate by
        their own secret. Without one configured the route itself refuses a
        non-loopback caller with its own 403 — assert the guard did not fire."""
        r = _client().post("/api/v1/webhooks/github", json={"zen": "ping"},
                           headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        detail = r.json().get("detail", "")
        assert detail != "cross-site request refused"
        assert "webhook secret not configured" in detail
