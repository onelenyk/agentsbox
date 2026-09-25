"""Keys & tokens: entered in the UI, kept in the keychain, read by config,
never inherited by a worker."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def kc(monkeypatch, tmp_path):
    """An in-memory keychain and a private .env for core.credentials."""
    from core import credentials, config
    store = {}
    monkeypatch.setattr(credentials, "_kc_read", lambda name: store.get(name))

    def write(name, value):
        if value is None:
            store.pop(name, None)
        else:
            store[name] = value
    monkeypatch.setattr(credentials, "_kc_write", write)
    env = tmp_path / ".env"
    monkeypatch.setattr(credentials, "_env_path", lambda: env)
    credentials._cache.clear()
    for n in ("JIRA_API_TOKEN", "JIRA_EMAIL", "JIRA_BASE_URL", "GITLAB_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    yield store
    credentials._cache.clear()
    config.reload_config()


def test_keychain_value_wins_over_environment(kc, monkeypatch):
    from core import credentials
    monkeypatch.setenv("GITLAB_TOKEN", "from-env-file")
    assert credentials.get("GITLAB_TOKEN") == "from-env-file"
    credentials.set_value("GITLAB_TOKEN", "glpat-fromthekeychain")
    assert credentials.get("GITLAB_TOKEN") == "glpat-fromthekeychain"
    assert credentials.source("GITLAB_TOKEN") == "keychain"
    credentials.clear("GITLAB_TOKEN")
    assert credentials.get("GITLAB_TOKEN") == "from-env-file"


def test_config_placeholder_reads_the_keychain_without_touching_os_environ(kc, monkeypatch):
    import os
    from core import credentials
    credentials.set_value("JIRA_API_TOKEN", "atl-token-123456")
    raw = "jira:\n  token: ${JIRA_API_TOKEN}\n"
    managed = credentials.managed_names(raw)
    assert credentials.resolve_placeholder("JIRA_API_TOKEN", managed) == "atl-token-123456"
    assert "JIRA_API_TOKEN" not in os.environ


def test_a_repo_token_named_in_config_is_managed(kc):
    from core import credentials
    raw = "repos:\n  - name: kmp\n    token: ${KMP_GITLAB_TOKEN}\nworker_env:\n  PATH: ${PATH}\n"
    names = credentials.managed_names(raw)
    assert "KMP_GITLAB_TOKEN" in names
    assert "PATH" not in names


def test_placeholders_that_are_not_credentials_never_hit_the_keychain(kc, monkeypatch):
    from core import credentials
    kc["PATH"] = "/evil"
    monkeypatch.setenv("PATH", "/usr/bin")
    assert credentials.resolve_placeholder("PATH", credentials.managed_names("")) == "/usr/bin"


def test_import_moves_env_file_values_and_comments_them_out(kc, monkeypatch):
    import os
    from core import credentials
    credentials._env_path().write_text(
        "# keep me\nJIRA_EMAIL=me@example.com\nJIRA_API_TOKEN='atl-secret-999'\nWORKSPACE_ROOT=/x\n")
    monkeypatch.setenv("JIRA_API_TOKEN", "atl-secret-999")
    assert set(credentials.env_file_pending()) == {"JIRA_EMAIL", "JIRA_API_TOKEN"}
    res = credentials.import_env_file()
    assert sorted(res["moved"]) == ["JIRA_API_TOKEN", "JIRA_EMAIL"]
    assert kc["JIRA_API_TOKEN"] == "atl-secret-999"
    text = credentials._env_path().read_text()
    assert "atl-secret-999" not in text and "me@example.com" not in text
    assert "# keep me" in text and "WORKSPACE_ROOT=/x" in text
    assert "JIRA_API_TOKEN" not in os.environ
    assert credentials.env_file_pending() == []


def test_view_masks_secrets_and_shows_addresses(kc):
    from core import credentials
    credentials.set_value("JIRA_API_TOKEN", "atl-token-abcdWXYZ")
    credentials.set_value("JIRA_BASE_URL", "https://team.atlassian.net")
    rows = {r["name"]: r for g in credentials.view() for r in g["rows"]}
    assert rows["JIRA_API_TOKEN"]["shown"] == "••••WXYZ"
    assert rows["JIRA_BASE_URL"]["shown"] == "https://team.atlassian.net"
    assert rows["GITLAB_TOKEN"]["set"] is False


def test_bad_names_and_values_are_refused(kc):
    from core import credentials
    with pytest.raises(credentials.CredentialError):
        credentials.set_value("lower", "x" * 10)
    with pytest.raises(credentials.CredentialError):
        credentials.set_value("JIRA_API_TOKEN", "  ")


def test_worker_env_drops_daemon_credentials(kc, monkeypatch):
    from core.launcher import _without_host_session
    env = _without_host_session({"JIRA_API_TOKEN": "t", "GITLAB_TOKEN": "g", "HOME": "/h", "SUPERSET_X": "1"})
    assert env == {"HOME": "/h"}


def test_keychain_rejects_values_that_would_break_the_security_line():
    from core import keychain
    assert keychain.valid_value("https://team.atlassian.net")
    assert keychain.valid_value("me@example.com")
    for bad in ('a"b', "a b", "a$b", "a\\b", "a`b", ""):
        assert not keychain.valid_value(bad)


# ── the page ────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path, kc):
    from web.app import app
    return TestClient(app)


def test_page_saves_a_token_and_never_echoes_it(client, kc):
    r = client.post("/api/credentials/JIRA_API_TOKEN", data={"value": "atl-token-SECRET1234", "action": "save"})
    assert r.status_code == 200
    assert kc["JIRA_API_TOKEN"] == "atl-token-SECRET1234"
    assert "atl-token-SECRET1234" not in r.text
    assert "••••1234" in r.text
    assert r.headers.get("HX-Trigger") == "integrationsUpdated"


def test_saved_jira_settings_make_jira_configured(client, kc):
    from core import jira
    for name, value in (("JIRA_BASE_URL", "https://team.atlassian.net"),
                        ("JIRA_EMAIL", "me@example.com"), ("JIRA_API_TOKEN", "atl-token-1234567")):
        assert client.post(f"/api/credentials/{name}", data={"value": value}).status_code == 200
    assert jira.configured()


def test_clear_removes_from_the_keychain(client, kc):
    client.post("/api/credentials/GITLAB_TOKEN", data={"value": "glpat-12345678"})
    r = client.post("/api/credentials/GITLAB_TOKEN", data={"action": "clear"})
    assert r.status_code == 200 and "GITLAB_TOKEN" not in kc


def test_cross_site_post_is_refused(client, kc):
    r = client.post("/api/credentials/JIRA_BASE_URL", data={"value": "https://evil.example"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert "JIRA_BASE_URL" not in kc


def test_unknown_name_is_refused(client, kc):
    r = client.post("/api/credentials/HOME", data={"value": "/tmp/x1234567"})
    assert "not a credential" in r.text
    assert "HOME" not in kc


def test_integrations_page_has_the_keys_section(client):
    r = client.get("/integrations")
    assert "credentials-view" in r.text
    v = client.get("/api/credentials/view")
    assert "Keys &amp; tokens" in v.text and "JIRA_API_TOKEN" in v.text
