"""Unit tests for core.repo_check + the routes that surface it.

No test here touches a network: the git and HTTP boundaries are injected
(``ls_remote=`` / ``http=``). The one exception is a local-path case that runs
the real ``git ls-remote`` against this very checkout — read-only, no network,
and it is the only way to prove the un-injected path works at all.
"""

import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.repo_check as rc


def _git_root() -> str:
    """The nearest real git repository above this file — reading it costs nothing.

    Walked rather than hard-coded: this package lives inside a checkout whose
    .git is a directory above it, and a wrong constant here would make the
    local-path tests assert against a plain directory.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return str(parent)
    raise RuntimeError("no git repository above the test file")


GIT_ROOT = _git_root()

MAIN_REFS = "abc123\trefs/heads/main\ndef456\trefs/heads/dev\n"


# ── stubs ────────────────────────────────────────────────────────────────

def ls_ok(refs=MAIN_REFS):
    return lambda url, timeout: {"ok": True, "timed_out": False, "detail": "", "refs": refs}


def ls_fail(detail="fatal: repository not found"):
    return lambda url, timeout: {"ok": False, "timed_out": False, "detail": detail, "refs": ""}


def ls_timeout():
    return lambda url, timeout: {"ok": False, "timed_out": True, "detail": "", "refs": ""}


def resp(status=200, body=None, offline=False, error=""):
    return {"status": status, "body": body, "offline": offline, "error": error}


def http_stub(routes, fallback=None):
    """Route by URL substring; record every call so tests can assert the target."""
    calls = []

    def _f(url, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        for frag, r in routes.items():
            if frag in url:
                return r
        return fallback or resp(404, error="no stub for " + url)

    _f.calls = calls
    return _f


def cfg(mapping):
    return lambda key, default=None: mapping.get(key, default)


GH_CFG = cfg({"github": {"token": "gh-token"}})
GL_CFG = cfg({"gitlab": {"token": "gl-token"}})
NO_CFG = cfg({})


def stages(res):
    return {s["name"]: s for s in res["stages"]}


# ── local repos ──────────────────────────────────────────────────────────

class TestLocal:
    """A local repo is a complete answer, not a degraded one."""

    def test_local_repo_reaches_tier_local(self):
        res = rc.check_connection(source=GIT_ROOT, branch="main", ls_remote=ls_ok())
        assert res["tier"] == "local"
        assert res["ok"] is True
        assert res["stage"] == ""
        st = stages(res)
        assert st["reachable"]["status"] == "ok"
        # No forge means no credential question — skipped, never "failed".
        assert st["authenticated"]["status"] == "skipped"
        assert st["can_push"]["status"] == "skipped"
        assert "patch" in res["summary"]

    def test_real_git_is_used_when_nothing_is_injected(self):
        """Exercises the un-injected _ls_remote — read-only, local, no network."""
        res = rc.check_connection(source=GIT_ROOT)
        assert res["tier"] == "local"
        assert res["ok"] is True

    def test_missing_path_fails_reachable_with_a_fix(self, tmp_path):
        res = rc.check_connection(source=str(tmp_path / "gone"), ls_remote=ls_ok())
        st = stages(res)["reachable"]
        assert st["status"] == "fail"
        assert "no such path" in st["detail"]
        assert st["fix"]
        assert res["tier"] == "unknown"
        assert res["ok"] is False
        assert res["stage"] == "reachable"

    def test_plain_directory_is_not_a_git_repo(self, tmp_path):
        res = rc.check_connection(source=str(tmp_path), ls_remote=ls_ok())
        st = stages(res)["reachable"]
        assert st["status"] == "fail"
        assert "not a git repository" in st["detail"]
        assert res["ok"] is False

    def test_blank_source_names_what_to_set(self):
        res = rc.check_connection(source="", ls_remote=ls_ok())
        st = stages(res)["reachable"]
        assert st["status"] == "fail"
        assert "no source configured" in st["detail"]
        assert "local path or a git URL" in st["fix"]


# ── reachable ────────────────────────────────────────────────────────────

class TestReachable:

    def test_timeout_is_unknown_not_failure(self):
        res = rc.check_connection(source="https://gitlab.com/o/r.git", platform="gitlab",
                                  ls_remote=ls_timeout(), http=http_stub({}))
        st = stages(res)["reachable"]
        assert st["status"] == "unknown"
        assert res["ok"] is False
        assert res["summary"].startswith("could not determine")

    def test_git_error_is_quoted_back(self):
        res = rc.check_connection(source="https://github.com/o/r.git", platform="github",
                                  ls_remote=ls_fail("fatal: could not read Username"),
                                  http=http_stub({}))
        st = stages(res)["reachable"]
        assert st["status"] == "fail"
        assert "could not read Username" in st["detail"]

    def test_missing_branch_is_noted_but_does_not_fail_the_stage(self):
        res = rc.check_connection(source=GIT_ROOT, branch="release",
                                  ls_remote=ls_ok(MAIN_REFS))
        st = stages(res)["reachable"]
        assert st["status"] == "ok"
        assert "'release' is not there" in st["detail"]
        assert "main" in st["detail"]
        # Still a usable local repo — the note is not a verdict.
        assert res["tier"] == "local"


# ── github ───────────────────────────────────────────────────────────────

GH_OK = {
    "/user": resp(200, {"login": "octocat"}),
    "/repos/o/r": resp(200, {"permissions": {"push": True, "pull": True}}),
}


def gh(source="https://github.com/o/r.git", routes=None, config=GH_CFG, **kw):
    http = http_stub(routes if routes is not None else GH_OK)
    with patch("core.config.get_config", config):
        res = rc.check_connection(source=source, platform="github",
                                  ls_remote=ls_ok(), http=http, **kw)
    res["_http"] = http
    return res


class TestGithub:

    def test_write_access_reaches_can_open_prs(self):
        res = gh()
        assert res["tier"] == "can_open_prs"
        assert res["ok"] is True
        assert res["identity"] == "octocat"
        st = stages(res)
        assert st["authenticated"]["status"] == "ok"
        assert "octocat" in st["authenticated"]["detail"]
        assert st["can_push"]["status"] == "ok"
        assert "octocat" in res["summary"]

    def test_read_only_token_fails_can_push(self):
        res = gh(routes={**GH_OK,
                         "/repos/o/r": resp(200, {"permissions": {"push": False, "pull": True}})})
        st = stages(res)["can_push"]
        assert st["status"] == "fail"
        assert "read-only" in st["detail"]
        assert "Write" in st["fix"] or "repo" in st["fix"]
        assert res["tier"] == "unknown"
        assert res["ok"] is False
        assert res["stage"] == "can_push"

    def test_maintain_counts_as_push(self):
        res = gh(routes={**GH_OK,
                         "/repos/o/r": resp(200, {"permissions": {"push": False, "maintain": True}})})
        assert stages(res)["can_push"]["status"] == "ok"
        assert res["tier"] == "can_open_prs"

    def test_missing_permissions_block_is_unknown_not_success(self):
        res = gh(routes={**GH_OK, "/repos/o/r": resp(200, {"name": "r"})})
        st = stages(res)["can_push"]
        assert st["status"] == "unknown"
        assert res["tier"] == "unknown"
        assert res["ok"] is False

    def test_repo_404_says_why_github_hides_it(self):
        res = gh(routes={**GH_OK, "/repos/o/r": resp(404, error="Not Found")})
        st = stages(res)["can_push"]
        assert st["status"] == "fail"
        assert "404" in st["detail"]
        assert "private" in st["fix"]

    def test_no_token_names_the_env_var(self):
        res = gh(config=NO_CFG)
        st = stages(res)
        assert st["authenticated"]["status"] == "fail"
        assert "GITHUB_TOKEN" in st["authenticated"]["fix"]
        # Nothing to ask about permissions with — skipped, not failed twice.
        assert st["can_push"]["status"] == "skipped"
        assert res["tier"] == "unknown"

    def test_rejected_token_says_expired_or_revoked(self):
        res = gh(routes={"/user": resp(401, error="Bad credentials")})
        st = stages(res)["authenticated"]
        assert st["status"] == "fail"
        assert "401" in st["detail"]
        assert "revoked" in st["fix"]

    def test_unreachable_forge_is_unknown_never_a_bad_token(self):
        res = gh(routes={"/user": resp(None, offline=True, error="Name or service not known")})
        st = stages(res)
        assert st["authenticated"]["status"] == "unknown"
        assert "says nothing about whether" in st["authenticated"]["fix"]
        # And permission was never even asked about.
        assert st["can_push"]["status"] == "unknown"
        assert "not attempted" in st["can_push"]["detail"]
        assert res["ok"] is False
        assert res["tier"] == "unknown"

    def test_self_hosted_base_url_is_honoured(self):
        res = gh(config=cfg({"github": {"token": "t", "base_url": "https://gh.corp/api/v3/"}}),
                 routes={"gh.corp": resp(200, {"login": "corp-bot", "permissions": {"push": True}})})
        urls = [c["url"] for c in res["_http"].calls]
        assert urls[0] == "https://gh.corp/api/v3/user"
        assert all(u.startswith("https://gh.corp/api/v3/") for u in urls)

    def test_placeholder_base_url_falls_back_to_public_api(self):
        res = gh(config=cfg({"github": {"token": "t", "base_url": "${GITHUB_BASE_URL}"}}))
        assert res["_http"].calls[0]["url"] == "https://api.github.com/user"

    def test_every_call_is_bounded_by_the_timeout(self):
        res = gh(timeout=3)
        assert res["_http"].calls
        assert all(c["timeout"] == 3 for c in res["_http"].calls)


# ── gitlab ───────────────────────────────────────────────────────────────

def gl(source="https://gitlab.com/g/p.git", routes=None, config=GL_CFG, **kw):
    http = http_stub(routes if routes is not None else {})
    with patch("core.config.get_config", config):
        res = rc.check_connection(source=source, platform="gitlab",
                                  ls_remote=ls_ok(), http=http, **kw)
    res["_http"] = http
    return res


def gl_routes(perms):
    return {"/api/v4/user": resp(200, {"username": "kate"}),
            "/api/v4/projects/": resp(200, {"permissions": perms})}


class TestGitlab:

    def test_developer_may_open_merge_requests(self):
        res = gl(routes=gl_routes({"project_access": {"access_level": 30}, "group_access": None}))
        assert res["tier"] == "can_open_prs"
        assert stages(res)["can_push"]["status"] == "ok"
        assert "Developer" in stages(res)["can_push"]["detail"]

    def test_reporter_cannot_and_the_fix_names_developer(self):
        res = gl(routes=gl_routes({"project_access": {"access_level": 20}, "group_access": None}))
        st = stages(res)["can_push"]
        assert st["status"] == "fail"
        assert "Reporter" in st["detail"]
        assert "Developer" in st["fix"]
        assert res["tier"] == "unknown"
        assert res["ok"] is False

    def test_group_membership_counts(self):
        res = gl(routes=gl_routes({"project_access": None, "group_access": {"access_level": 40}}))
        assert stages(res)["can_push"]["status"] == "ok"
        assert "Maintainer" in stages(res)["can_push"]["detail"]

    def test_no_membership_level_at_all_is_unknown(self):
        res = gl(routes=gl_routes({"project_access": None, "group_access": None}))
        st = stages(res)["can_push"]
        assert st["status"] == "unknown"
        assert res["ok"] is False

    def test_nested_project_path_is_url_encoded(self):
        res = gl(source="https://gitlab.com/grp/sub/proj.git",
                 routes=gl_routes({"project_access": {"access_level": 50}}))
        project_calls = [c["url"] for c in res["_http"].calls if "/projects/" in c["url"]]
        assert project_calls == ["https://gitlab.com/api/v4/projects/grp%2Fsub%2Fproj"]

    def test_token_travels_in_the_gitlab_header(self):
        res = gl(routes=gl_routes({"project_access": {"access_level": 30}}))
        assert all(c["headers"].get("PRIVATE-TOKEN") == "gl-token" for c in res["_http"].calls)


# ── tier is derived, never taken from the form ───────────────────────────

class TestTier:

    def test_platform_the_user_typed_is_never_evidence(self):
        """A repo labelled github that proved nothing is unknown, not github."""
        res = gh(config=NO_CFG, routes={})
        assert res["platform"] == "github"
        assert res["tier"] == "unknown"
        assert res["ok"] is False

    def test_remote_on_a_forge_with_no_publisher_is_not_success(self):
        res = rc.check_connection(source="https://bitbucket.org/o/r.git",
                                  ls_remote=ls_ok(), http=http_stub({}))
        assert res["platform"] == "unknown"
        assert stages(res)["reachable"]["status"] == "ok"
        assert res["tier"] == "unknown"
        assert res["ok"] is False
        assert "no publisher" in res["summary"]

    def test_path_registered_forge_repo_without_origin_says_so(self, tmp_path):
        with patch("core.providers._resolve_forge_url", return_value=None), \
             patch("core.config.get_config", GH_CFG):
            res = rc.check_connection(source=GIT_ROOT, platform="github",
                                      ls_remote=ls_ok(),
                                      http=http_stub({"/user": resp(200, {"login": "x"})}))
        st = stages(res)["can_push"]
        assert st["status"] == "fail"
        assert "origin" in st["detail"]
        assert "platform to local" in st["fix"]
        assert res["tier"] == "unknown"


# ── credentials ──────────────────────────────────────────────────────────

class TestCredential:

    def test_a_repos_own_token_wins_and_is_named_as_such(self):
        repo = {"name": "alpha", "source": "https://github.com/o/r.git",
                "platform": "github", "branch": "main", "token": "repo-tok"}
        http = http_stub(GH_OK)
        with patch("core.repos.load_repos", return_value=[repo]), \
             patch("core.config.get_config", GH_CFG):
            res = rc.check_connection(name="alpha", ls_remote=ls_ok(), http=http)
        assert res["tier"] == "can_open_prs"
        assert "repo token" in stages(res)["authenticated"]["detail"]
        assert all(c["headers"]["Authorization"] == "Bearer repo-tok" for c in http.calls)

    def test_saved_repo_fields_are_read_when_only_a_name_is_given(self):
        repo = {"name": "alpha", "source": GIT_ROOT, "platform": "", "branch": "main"}
        with patch("core.repos.load_repos", return_value=[repo]):
            res = rc.check_connection(name="alpha", ls_remote=ls_ok())
        assert res["source"] == GIT_ROOT
        assert res["platform"] == "local"
        assert res["tier"] == "local"

    def test_unsaved_form_still_finds_the_global_token(self):
        """The add form asks before there is a repo row; the global block answers."""
        http = http_stub(GH_OK)
        with patch("core.repos.load_repos", return_value=[]), \
             patch("core.config.get_config", GH_CFG):
            res = rc.check_connection(name="not-saved-yet",
                                      source="https://github.com/o/r.git",
                                      platform="github", ls_remote=ls_ok(), http=http)
        assert res["tier"] == "can_open_prs"
        assert "global token" in stages(res)["authenticated"]["detail"]

    def test_unresolved_placeholder_is_not_a_token(self):
        res = gh(config=cfg({"github": {"token": "${GITHUB_TOKEN}"}}))
        st = stages(res)["authenticated"]
        assert st["status"] == "fail"
        assert "GITHUB_TOKEN" in st["fix"]


# ── the I/O boundary itself ──────────────────────────────────────────────

class TestGitBoundary:

    def test_ls_remote_is_read_only_and_cannot_prompt(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, env=kw.get("env"), timeout=kw.get("timeout"))
            return SimpleNamespace(returncode=0, stdout=MAIN_REFS, stderr="")

        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        monkeypatch.setattr(rc.subprocess, "run", fake_run)
        out = rc._ls_remote("git@example.com:o/r.git", 7)

        assert seen["argv"] == ["git", "ls-remote", "--heads", "git@example.com:o/r.git"]
        assert seen["timeout"] == 7
        # Without these git blocks on a username prompt against a private URL,
        # and the timeout would then be reported as a network problem.
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]
        assert out["ok"] is True and out["refs"] == MAIN_REFS

    def test_ls_remote_timeout_is_flagged_separately_from_failure(self, monkeypatch):
        import subprocess as sp

        def boom(argv, **kw):
            raise sp.TimeoutExpired(argv, kw.get("timeout", 1))

        monkeypatch.setattr(rc.subprocess, "run", boom)
        out = rc._ls_remote("https://example.com/r.git", 1)
        assert out["timed_out"] is True and out["ok"] is False


class TestHttpBoundary:

    def test_url_error_is_offline_not_a_status(self, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.URLError("nodename nor servname provided")

        monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
        out = rc._http_get("https://api.github.com/user", {}, 5)
        assert out["offline"] is True and out["status"] is None

    def test_socket_timeout_is_offline(self, monkeypatch):
        def boom(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
        out = rc._http_get("https://api.github.com/user", {}, 5)
        assert out["offline"] is True

    def test_http_error_keeps_its_status_and_body(self, monkeypatch):
        import io

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"message":"Bad credentials"}'))

        monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
        out = rc._http_get("https://api.github.com/user", {}, 5)
        assert out["status"] == 401
        assert out["offline"] is False
        assert out["body"] == {"message": "Bad credentials"}


# ── routes ───────────────────────────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from web.app import app
    return TestClient(app)


LOCAL_RESULT = {
    "repo": "alpha", "source": "/tmp/alpha", "platform": "local", "ok": True,
    "tier": "local", "stage": "", "detail": "", "summary": "local repo — reachable",
    "identity": "", "duration_ms": 12,
    "stages": [{"name": "reachable", "status": "ok", "detail": "git can read it", "fix": ""},
               {"name": "authenticated", "status": "skipped", "detail": "no forge", "fix": ""},
               {"name": "can_push", "status": "skipped", "detail": "patch", "fix": ""}],
}

FAILED_RESULT = {
    **LOCAL_RESULT, "ok": False, "tier": "unknown", "stage": "authenticated",
    "detail": "no github token for this repo", "summary": "failed at authenticated",
    "stages": [{"name": "reachable", "status": "ok", "detail": "ok", "fix": ""},
               {"name": "authenticated", "status": "fail",
                "detail": "no github token for this repo", "fix": "set GITHUB_TOKEN"},
               {"name": "can_push", "status": "skipped", "detail": "", "fix": ""}],
}


class TestRoutes:

    def test_check_renders_the_stages(self, db_path):
        with patch("core.repos.load_repos", return_value=[{"name": "alpha"}]), \
             patch("core.repo_check.check_connection", return_value=LOCAL_RESULT):
            r = _client().post("/api/repos/alpha/check")
        assert r.status_code == 200
        for token in ("local", "reachable", "authenticated", "can_push", "skipped"):
            assert token in r.text

    def test_a_failure_renders_the_fix_and_never_reads_as_success(self, db_path):
        with patch("core.repos.load_repos", return_value=[{"name": "alpha"}]), \
             patch("core.repo_check.check_connection", return_value=FAILED_RESULT):
            r = _client().post("/api/repos/alpha/check")
        assert r.status_code == 200
        assert "set GITHUB_TOKEN" in r.text
        assert "unknown" in r.text
        assert "status--failed" in r.text
        assert "status--done" not in r.text

    def test_unknown_repo_reports_on_the_hx_trigger_channel(self, db_path):
        with patch("core.repos.load_repos", return_value=[]):
            r = _client().post("/api/repos/ghost/check")
        assert r.status_code == 400
        assert "repoError" in r.headers["HX-Trigger"]
        assert "ghost" in r.headers["HX-Trigger"]

    def test_a_crashing_check_is_reported_not_a_500(self, db_path):
        with patch("core.repos.load_repos", return_value=[{"name": "alpha"}]), \
             patch("core.repo_check.check_connection", side_effect=RuntimeError("git vanished")):
            r = _client().post("/api/repos/alpha/check")
        assert r.status_code == 400
        assert "git vanished" in r.headers["HX-Trigger"]

    def test_form_check_runs_before_anything_is_saved(self, db_path):
        seen = {}

        def spy(**kw):
            seen.update(kw)
            return LOCAL_RESULT

        with patch("core.repo_check.check_connection", side_effect=spy):
            r = _client().post("/api/repos/check",
                               data={"name": "brand-new", "source": " /srv/x ",
                                     "platform": "gitlab", "branch": "trunk"})
        assert r.status_code == 200
        assert seen == {"name": "brand-new", "source": "/srv/x",
                        "platform": "gitlab", "branch": "trunk"}
        # No repo was created by asking.
        with patch("core.repos.load_repos", return_value=[]):
            import core.repos
            assert core.repos.get_repo("brand-new") is None
