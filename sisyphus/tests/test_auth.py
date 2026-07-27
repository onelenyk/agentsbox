"""Tests for the /api/v1 shared-token guard (web/auth.py)."""
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from core import config
from web.app import app
from web import auth


@contextmanager
def _api_token(value):
    """Enable API auth by setting the env the config placeholder resolves from,
    then reload the cached config. Always restores on exit."""
    prev = os.environ.get("SISYPHUS_API_TOKEN")
    if value is None:
        os.environ.pop("SISYPHUS_API_TOKEN", None)
    else:
        os.environ["SISYPHUS_API_TOKEN"] = value
    config.reload_config()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("SISYPHUS_API_TOKEN", None)
        else:
            os.environ["SISYPHUS_API_TOKEN"] = prev
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
    MR URL and simply ignores anything it can't place)."""
    with _api_token("s3cret"):
        r = _client().post("/api/v1/webhooks/github", json={"zen": "ping"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "ignored": True}


def test_non_api_routes_unaffected(db_path):
    """The guard only covers /api/v1 — the htmx UI is untouched."""
    with _api_token("s3cret"):
        # /tasks renders HTML; just assert it isn't a 401
        assert _client().get("/tasks").status_code != 401


class TestLocalhostExemption:
    class _FakeReq:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()

    def test_loopback_is_local(self):
        assert auth._is_local(self._FakeReq("127.0.0.1"))
        assert auth._is_local(self._FakeReq("::1"))

    def test_remote_is_not_local(self):
        assert not auth._is_local(self._FakeReq("10.0.0.4"))
        assert not auth._is_local(self._FakeReq("testclient"))
