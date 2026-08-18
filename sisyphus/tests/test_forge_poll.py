"""Tests for core/forge_poll.py — learning what happened to a PR by asking.

No test here touches the network: `forge_poll.http_get` is the module's entire
network surface and every test replaces it with a recorder, so a test that
accidentally reaches a forge fails on an unexpected call rather than passing
slowly.
"""
import json
from unittest.mock import patch

import pytest

from core import forge_poll, mergerequests, store


# ── fakes ───────────────────────────────────────────────────────────────


class FakeForge:
    """Answers GETs from a {url-suffix: payload} table and records every call.

    A payload may be an Exception, which is raised — that is how "the forge is
    down" is expressed. An unmatched URL raises, so a test that expects one
    request and gets three fails loudly.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers, timeout=20):
        self.calls.append((url, headers))
        # endswith, not "in": "/pulls/7" is a substring of the reviews URL too,
        # and matching it there would hand a PR object to the review parser.
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise forge_poll.ForgeError(f"HTTP 404 (unrouted {url})")


@pytest.fixture
def forge(monkeypatch):
    """Install a FakeForge; the routes are filled in by each test."""
    f = FakeForge({})
    monkeypatch.setattr(forge_poll, "http_get", f)
    forge_poll.reset_state()
    yield f
    forge_poll.reset_state()


def _pr(**kw):
    base = {"state": "open", "merged": False, "mergeable_state": "clean",
            "updated_at": "2026-08-01T00:00:00Z", "comments": 0, "review_comments": 0}
    base.update(kw)
    return base


def _review(login, state):
    return {"user": {"login": login}, "state": state}


def _setup_mr(db_path, repo="acme", url="https://github.com/acme/widgets/pull/7",
              status="active", record_url=True):
    """A task that opened a PR, plus the MR row the poller reads."""
    task = store.create_task("ship the thing", repo=repo)
    if record_url:
        store.post_message(task["id"], "engineer", kind="mr", text=f"My change: {url}")
    if status != "active":
        store.set_status(task["id"], status)
    mr = mergerequests.create_mr(task_id=task["id"], repo=repo,
                                 source_branch=f"sisyphus/{task['id']}",
                                 target_branch="main", title="My change")
    return task, mr


def _github_repo(token="tok"):
    """Patch repo lookup so repo_credential resolves a real GitHub token."""
    return patch("core.repos.get_repo",
                 return_value={"name": "acme", "source": "https://github.com/acme/widgets.git",
                               "platform": "github", "token": token})


def _external(task_id):
    return [m["text"] for m in store.get_thread(task_id) if m["kind"] == "external"]


# ── URL parsing ─────────────────────────────────────────────────────────


class TestParseForgeUrl:
    def test_github(self):
        assert forge_poll.parse_forge_url("https://github.com/acme/widgets/pull/7") == \
            ("github", "acme/widgets", 7)

    def test_github_enterprise_host(self):
        assert forge_poll.parse_forge_url("https://git.corp.example/acme/widgets/pull/12") == \
            ("github", "acme/widgets", 12)

    def test_gitlab_modern_and_legacy(self):
        assert forge_poll.parse_forge_url("https://gitlab.com/g/sub/proj/-/merge_requests/45") == \
            ("gitlab", "g/sub/proj", 45)
        assert forge_poll.parse_forge_url("https://gitlab.com/g/proj/merge_requests/3") == \
            ("gitlab", "g/proj", 3)

    def test_rejects_non_mr_urls(self):
        assert forge_poll.parse_forge_url("https://github.com/acme/widgets/issues/7") is None
        assert forge_poll.parse_forge_url("https://github.com/acme/widgets") is None
        assert forge_poll.parse_forge_url("") is None
        assert forge_poll.parse_forge_url(None) is None


class TestForgeUrlFor:
    def test_prefers_stored_column(self, db_path):
        _task, mr = _setup_mr(db_path, url="https://github.com/acme/widgets/pull/7")
        mergerequests.set_forge_url(mr["id"], "https://github.com/acme/widgets/pull/99")
        mr = mergerequests.get_mr(mr["id"])
        assert forge_poll.forge_url_for(mr) == "https://github.com/acme/widgets/pull/99"

    def test_recovers_from_thread_and_persists(self, db_path):
        url = "https://github.com/acme/widgets/pull/7"
        _task, mr = _setup_mr(db_path, url=url)
        assert forge_poll.forge_url_for(mr) == url
        # Persisted, so the thread scan happens once per MR rather than per tick.
        assert mergerequests.get_mr(mr["id"])["forge_url"] == url

    def test_none_when_thread_has_no_forge_url(self, db_path):
        _task, mr = _setup_mr(db_path, record_url=False)
        assert forge_poll.forge_url_for(mr) is None


# ── reading state ───────────────────────────────────────────────────────


class TestGithubState:
    def test_merged_is_not_just_closed(self, forge):
        forge.routes = {"/pulls/7": _pr(state="closed", merged=True), "/reviews?per_page=100": []}
        st = forge_poll._github_state("acme/widgets", 7, "tok", None)
        assert st["state"] == "merged"

    def test_latest_verdict_per_reviewer_counts(self, forge):
        forge.routes = {
            "/pulls/7": _pr(),
            "/reviews?per_page=100": [
                _review("ann", "APPROVED"),
                _review("bob", "CHANGES_REQUESTED"),
                _review("ann", "CHANGES_REQUESTED"),   # ann changed her mind
                _review("cat", "COMMENTED"),           # not a verdict
                _review("bob", "APPROVED"),
            ],
        }
        st = forge_poll._github_state("acme/widgets", 7, "tok", None)
        assert st["approvals"] == 1          # bob only; ann's latest is a rejection
        assert st["review"] == "approved"

    def test_comment_counts_are_summed(self, forge):
        forge.routes = {"/pulls/7": _pr(comments=2, review_comments=3),
                        "/reviews?per_page=100": []}
        assert forge_poll._github_state("acme/widgets", 7, "tok", None)["comments"] == 5

    def test_unchanged_updated_at_skips_the_reviews_call(self, forge):
        previous = {"updated_at": "2026-08-01T00:00:00Z", "approvals": 2, "review": "approved",
                    "state": "open", "merge": "clean", "comments": 0}
        forge.routes = {"/pulls/7": _pr()}     # no /reviews route: calling it would 404
        st = forge_poll._github_state("acme/widgets", 7, "tok", previous)
        assert len(forge.calls) == 1
        assert st["approvals"] == 2 and st["review"] == "approved"

    def test_changed_updated_at_refetches_reviews(self, forge):
        previous = {"updated_at": "2026-07-01T00:00:00Z", "approvals": 0}
        forge.routes = {"/pulls/7": _pr(), "/reviews?per_page=100": [_review("ann", "APPROVED")]}
        st = forge_poll._github_state("acme/widgets", 7, "tok", previous)
        assert len(forge.calls) == 2
        assert st["approvals"] == 1


class TestGitlabState:
    def test_opened_is_normalised_to_open(self, forge):
        forge.routes = {"/merge_requests/45": {"state": "opened", "merge_status": "can_be_merged",
                                               "updated_at": "t1", "user_notes_count": 4},
                        "/45/approvals": {"approved_by": [{"user": {"username": "ann"}}]}}
        st = forge_poll._gitlab_state("g/proj", 45, "tok", None)
        assert st["state"] == "open"
        assert st["comments"] == 4
        assert st["approvals"] == 1

    def test_missing_approvals_endpoint_does_not_fail_the_read(self, forge):
        # Approval rules are a paid feature; a 404 there means "no rules", and
        # must not discard the MR state we already read successfully.
        forge.routes = {"/merge_requests/45": {"state": "merged", "updated_at": "t1"}}
        st = forge_poll._gitlab_state("g/proj", 45, "tok", None)
        assert st["state"] == "merged"
        assert st["approvals"] == 0


# ── what counts as a change ─────────────────────────────────────────────


class TestDiffState:
    def test_first_sight_of_an_open_pr_is_not_news(self):
        current = {"state": "open", "approvals": 3, "comments": 12, "merge": "clean"}
        assert forge_poll.diff_state("github", None, current) == []

    def test_first_sight_of_a_finished_pr_is_news(self):
        out = forge_poll.diff_state("github", None, {"state": "merged"})
        assert out == ["[external] GitHub PR merged"]

    def test_no_change_reports_nothing(self):
        st = {"state": "open", "merge": "clean", "updated_at": "t", "comments": 3,
              "approvals": 1, "review": "approved"}
        assert forge_poll.diff_state("github", dict(st), dict(st)) == []

    def test_merge_close_and_reopen(self):
        prev = {"state": "open"}
        assert forge_poll.diff_state("github", prev, {"state": "merged"}) == \
            ["[external] GitHub PR merged"]
        assert forge_poll.diff_state("github", prev, {"state": "closed"}) == \
            ["[external] GitHub PR closed"]
        assert forge_poll.diff_state("github", {"state": "closed"}, {"state": "open"}) == \
            ["[external] GitHub PR reopened"]

    def test_new_approval_and_changes_requested(self):
        prev = {"state": "open", "approvals": 0, "review": ""}
        out = forge_poll.diff_state("github", prev, {"state": "open", "approvals": 2,
                                                     "review": "approved"})
        assert out == ["[external] GitHub review: approved (2 approval(s))"]
        out = forge_poll.diff_state("github", prev, {"state": "open", "approvals": 0,
                                                     "review": "changes_requested"})
        assert out == ["[external] GitHub review: changes_requested"]

    def test_changes_requested_is_not_repeated(self):
        prev = {"state": "open", "review": "changes_requested"}
        assert forge_poll.diff_state("github", prev, dict(prev)) == []

    def test_only_the_new_comments_are_counted(self):
        out = forge_poll.diff_state("gitlab", {"state": "open", "comments": 4},
                                    {"state": "open", "comments": 7})
        assert out == ["[external] GitLab MR: 3 new comment(s)"]

    def test_deleted_comments_do_not_report_negative_news(self):
        assert forge_poll.diff_state("github", {"state": "open", "comments": 7},
                                     {"state": "open", "comments": 4}) == []

    def test_becoming_conflicted_is_reported_once(self):
        prev = {"state": "open", "merge": "clean"}
        out = forge_poll.diff_state("github", prev, {"state": "open", "merge": "dirty"})
        assert out == ["[external] GitHub PR has merge conflicts with the target branch"]
        assert forge_poll.diff_state("github", {"state": "open", "merge": "dirty"},
                                     {"state": "open", "merge": "dirty"}) == []

    def test_gitlab_wording(self):
        assert forge_poll.diff_state("gitlab", {"state": "open"}, {"state": "merged"}) == \
            ["[external] GitLab MR merged"]

    def test_external_prefix_matches_the_webhook_path(self):
        # A thread must not reveal which transport delivered the news, so the
        # summaries carry the same prefix core.webhooks puts on every event.
        for line in forge_poll.diff_state("github", {"state": "open", "comments": 0},
                                          {"state": "merged", "comments": 2}):
            assert line.startswith("[external] ")


# ── one pass ────────────────────────────────────────────────────────────


class TestPollOnce:
    def test_a_merge_reaches_the_task_thread_and_wakes_it(self, db_path, forge):
        task, mr = _setup_mr(db_path, status="done")
        mergerequests.save_forge_state(mr["id"], {"state": "open", "merge": "clean",
                                                  "updated_at": "t0", "comments": 0,
                                                  "approvals": 0, "review": ""})
        forge.routes = {"/pulls/7": _pr(state="closed", merged=True, updated_at="t1"),
                        "/reviews?per_page=100": [_review("ann", "APPROVED")]}
        with _github_repo():
            summary = forge_poll.poll_once()

        assert summary["checked"] == 1 and summary["changed"] == 1
        assert summary["errors"] == [] and summary["skipped"] == []
        assert "[external] GitHub PR merged" in _external(task["id"])
        # Exactly what a webhook does: a finished task is put back to work.
        assert store.get_task(task["id"])["status"] == "active"
        # …and the local row stops being polled forever.
        assert mergerequests.get_mr(mr["id"])["status"] == "merged"

    def test_unchanged_state_posts_nothing(self, db_path, forge):
        task, mr = _setup_mr(db_path)
        forge.routes = {"/pulls/7": _pr(), "/reviews?per_page=100": []}
        with _github_repo():
            first = forge_poll.poll_once()
            second = forge_poll.poll_once()
        assert first["changed"] == 0     # first sight of an open PR is a baseline
        assert second["changed"] == 0
        assert _external(task["id"]) == []

    def test_a_review_arriving_between_ticks_is_reported_once(self, db_path, forge):
        task, _mr = _setup_mr(db_path)
        forge.routes = {"/pulls/7": _pr(), "/reviews?per_page=100": []}
        with _github_repo():
            forge_poll.poll_once()                       # baseline
            forge.routes = {"/pulls/7": _pr(updated_at="t2", comments=1),
                            "/reviews?per_page=100": [_review("ann", "APPROVED")]}
            forge_poll.poll_once()
            forge_poll.poll_once()                       # nothing new since

        assert _external(task["id"]) == [
            "[external] GitHub review: approved (1 approval(s))",
            "[external] GitHub PR: 1 new comment(s)",
        ]

    def test_local_repos_are_never_polled(self, db_path, forge):
        _setup_mr(db_path, repo="localrepo", record_url=False)
        with patch("core.repos.get_repo",
                   return_value={"name": "localrepo", "source": "/Users/me/proj"}):
            summary = forge_poll.poll_once()
        assert forge.calls == []
        assert summary == {"checked": 0, "changed": 0, "events": [], "skipped": [], "errors": []}

    def test_closed_mrs_are_not_polled(self, db_path, forge):
        _task, mr = _setup_mr(db_path)
        mergerequests.set_status(mr["id"], "closed")
        with _github_repo():
            summary = forge_poll.poll_once()
        assert forge.calls == []
        assert summary["checked"] == 0

    def test_mr_without_a_forge_url_is_skipped_not_guessed(self, db_path, forge):
        _task, mr = _setup_mr(db_path, record_url=False)
        with _github_repo():
            summary = forge_poll.poll_once()
        assert forge.calls == []
        assert summary["skipped"][0]["mr"] == mr["id"]
        assert "no forge URL" in summary["skipped"][0]["reason"]

    def test_one_broken_mr_does_not_end_the_pass(self, db_path, forge):
        _t1, _mr1 = _setup_mr(db_path, url="https://github.com/acme/widgets/pull/7")
        t2, _mr2 = _setup_mr(db_path, url="https://github.com/acme/widgets/pull/8")
        forge.routes = {"/pulls/7": RuntimeError("kaboom"),
                        "/pulls/8": _pr(state="closed", merged=True),
                        "/reviews?per_page=100": []}
        with _github_repo():
            summary = forge_poll.poll_once()
        assert summary["checked"] == 1
        assert any("kaboom" in e["error"] for e in summary["errors"])
        assert "[external] GitHub PR merged" in _external(t2["id"])


class TestFailuresAreNotSilence:
    def test_an_unreachable_forge_is_an_error_not_an_unchanged_pr(self, db_path, forge):
        task, mr = _setup_mr(db_path)
        baseline = {"state": "open", "merge": "clean", "updated_at": "t0", "comments": 0,
                    "approvals": 0, "review": ""}
        mergerequests.save_forge_state(mr["id"], baseline)
        checked_at = mergerequests.get_mr(mr["id"])["forge_checked_at"]
        forge.routes = {"/pulls/7": forge_poll.ForgeError("unreachable: timed out")}
        with _github_repo():
            summary = forge_poll.poll_once()

        assert summary["checked"] == 0 and summary["changed"] == 0
        assert "unreachable" in summary["errors"][0]["error"]
        assert _external(task["id"]) == []
        # The baseline must survive untouched — overwriting it here is how a
        # merge that happened during the outage would never be reported. Nothing
        # at all may be written: forge_checked_at means "when we last actually
        # saw this PR", and a failed read that stamps it claims an observation
        # that never happened.
        assert mergerequests.get_forge_state(mr["id"]) == baseline
        assert mergerequests.get_mr(mr["id"])["forge_checked_at"] == checked_at

    def test_the_missed_change_is_reported_on_the_next_successful_tick(self, db_path, forge):
        task, mr = _setup_mr(db_path)
        mergerequests.save_forge_state(mr["id"], {"state": "open", "merge": "clean",
                                                  "updated_at": "t0", "comments": 0,
                                                  "approvals": 0, "review": ""})
        with _github_repo():
            forge.routes = {"/pulls/7": forge_poll.ForgeError("HTTP 502 bad gateway")}
            forge_poll.poll_once()
            assert _external(task["id"]) == []
            forge.routes = {"/pulls/7": _pr(state="closed", merged=True, updated_at="t9"),
                            "/reviews?per_page=100": []}
            forge_poll.poll_once()
        assert _external(task["id"]) == ["[external] GitHub PR merged"]

    def test_rate_limit_parks_the_host_instead_of_earning_more_403s(self, db_path, forge):
        _t1, _mr1 = _setup_mr(db_path, url="https://github.com/acme/widgets/pull/7")
        _t2, _mr2 = _setup_mr(db_path, url="https://github.com/acme/widgets/pull/8")
        limited = forge_poll.ForgeError("HTTP 403 rate limited", retry_after=600)
        forge.routes = {"/pulls/7": limited, "/pulls/8": limited}
        with _github_repo():
            summary = forge_poll.poll_once()
        # One request, two MRs: the second was not attempted at all.
        assert len(forge.calls) == 1
        assert len(summary["errors"]) == 2
        assert any("rate-limited" in e["error"] for e in summary["errors"])

        with _github_repo():                    # still parked on the next tick
            forge_poll.poll_once()
        assert len(forge.calls) == 1

    def test_reset_state_clears_the_cooldown(self, db_path, forge):
        _setup_mr(db_path)
        forge.routes = {"/pulls/7": forge_poll.ForgeError("HTTP 429", retry_after=600)}
        with _github_repo():
            forge_poll.poll_once()
            forge_poll.reset_state()
            forge_poll.poll_once()
        assert len(forge.calls) == 2

    def test_retry_after_header_is_honoured(self):
        class E(Exception):
            headers = {"Retry-After": "120"}
        assert forge_poll._retry_after(E()) == 120.0

    def test_exhausted_github_quota_waits_for_the_reset(self):
        import time as _t

        class E(Exception):
            headers = {"X-RateLimit-Remaining": "0",
                       "X-RateLimit-Reset": str(int(_t.time()) + 300)}
        assert 250 < forge_poll._retry_after(E()) <= 300

    def test_an_ordinary_error_carries_no_cooldown(self):
        class E(Exception):
            headers = {"X-RateLimit-Remaining": "4999"}
        assert forge_poll._retry_after(E()) is None


class TestMissingCredentials:
    def test_skipped_with_the_variable_that_would_fix_it(self, db_path, forge, caplog):
        _task, mr = _setup_mr(db_path)
        no_token = patch("core.repos.get_repo",
                         return_value={"name": "acme", "platform": "github",
                                       "source": "https://github.com/acme/widgets.git"})
        with no_token, patch("core.config.get_config", return_value={}), \
                caplog.at_level("WARNING"):
            summary = forge_poll.poll_once()

        assert forge.calls == []
        skipped = summary["skipped"][0]
        assert skipped["mr"] == mr["id"]
        assert skipped["env_hint"] == "GITHUB_TOKEN"
        assert "GITHUB_TOKEN" in skipped["reason"]
        assert any("GITHUB_TOKEN" in r.getMessage() for r in caplog.records)

    def test_the_warning_is_not_repeated_every_tick(self, db_path, forge, caplog):
        _setup_mr(db_path)
        no_token = patch("core.repos.get_repo",
                         return_value={"name": "acme", "platform": "github",
                                       "source": "https://github.com/acme/widgets.git"})
        with no_token, patch("core.config.get_config", return_value={}), \
                caplog.at_level("WARNING"):
            for _ in range(5):
                summary = forge_poll.poll_once()
        # Still reported every pass in the summary — only the log is de-duplicated,
        # because a line per repo per tick would bury the one that names the fix.
        assert len(summary["skipped"]) == 1
        assert len([r for r in caplog.records if "forge_poll" in r.getMessage()]) == 1


class TestReadOnly:
    def test_the_http_layer_only_ever_issues_get(self, monkeypatch):
        seen = {}

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            return FakeResp()

        monkeypatch.setattr(forge_poll.urllib.request, "urlopen", fake_urlopen)
        forge_poll._http_get("https://api.github.com/repos/a/b/pulls/1", {})
        assert seen["method"] == "GET"

    def test_transport_errors_all_arrive_as_forge_error(self, monkeypatch):
        import urllib.error

        for exc in (urllib.error.URLError("boom"),
                    urllib.error.HTTPError("u", 500, "err", {}, None),
                    ValueError("not json")):
            monkeypatch.setattr(forge_poll.urllib.request, "urlopen",
                                lambda *a, e=exc, **kw: (_ for _ in ()).throw(e))
            with pytest.raises(forge_poll.ForgeError):
                forge_poll._http_get("https://api.github.com/x", {})


# ── config ──────────────────────────────────────────────────────────────


class TestConfig:
    def test_default_interval(self):
        with patch("core.config.get_config", side_effect=lambda k, d=None: d):
            assert forge_poll.poll_interval() == forge_poll.DEFAULT_INTERVAL

    def test_a_too_fast_interval_is_floored(self):
        with patch("core.config.get_config", return_value=5):
            assert forge_poll.poll_interval() == forge_poll.MIN_INTERVAL

    def test_a_nonsense_interval_falls_back(self):
        with patch("core.config.get_config", return_value="soon"):
            assert forge_poll.poll_interval() == forge_poll.DEFAULT_INTERVAL

    def test_self_hosted_base_url_is_used(self):
        with patch("core.config.get_config", return_value={"base_url": "https://gl.corp/"}):
            assert forge_poll._api_base("gitlab") == "https://gl.corp"

    def test_unresolved_placeholder_is_not_an_address(self):
        with patch("core.config.get_config", return_value={"base_url": "${GITHUB_BASE_URL}"}):
            assert forge_poll._api_base("github") == "https://api.github.com"

    def test_enabled_by_default(self):
        with patch("core.config.get_config", side_effect=lambda k, d=None: d):
            assert forge_poll.poll_enabled() is True


class TestForgeStatePersistence:
    def test_roundtrip(self, db_path):
        _task, mr = _setup_mr(db_path)
        assert mergerequests.get_forge_state(mr["id"]) is None
        mergerequests.save_forge_state(mr["id"], {"state": "open", "approvals": 2})
        assert mergerequests.get_forge_state(mr["id"]) == {"state": "open", "approvals": 2}
        assert mergerequests.get_mr(mr["id"])["forge_checked_at"]

    def test_never_observed_and_observed_empty_are_different(self, db_path):
        _task, mr = _setup_mr(db_path)
        mergerequests.save_forge_state(mr["id"], {})
        # `{}` is falsy but is still a real observation; None means we never looked.
        assert mergerequests.get_forge_state(mr["id"]) is not None

    def test_corrupt_state_reads_as_never_observed(self, db_path):
        _task, mr = _setup_mr(db_path)
        from core.db import get_conn
        conn = get_conn()
        conn.execute("UPDATE merge_requests SET forge_state = ? WHERE id = ?",
                     ("{not json", mr["id"]))
        conn.commit()
        conn.close()
        assert mergerequests.get_forge_state(mr["id"]) is None


class TestPollForever:
    def test_disabled_returns_immediately(self):
        import asyncio
        with patch.object(forge_poll, "poll_enabled", return_value=False), \
                patch.object(forge_poll, "poll_once", side_effect=AssertionError("polled!")):
            asyncio.run(forge_poll.poll_forever())

    def test_a_failing_pass_does_not_kill_the_loop(self):
        import asyncio
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("forge exploded")
            raise KeyboardInterrupt        # a way out of the infinite loop

        async def run():
            with patch.object(forge_poll, "poll_enabled", return_value=True), \
                    patch.object(forge_poll, "poll_interval", return_value=0), \
                    patch.object(forge_poll, "poll_once", side_effect=boom), \
                    patch("asyncio.sleep", side_effect=_noop_sleep):
                with pytest.raises(KeyboardInterrupt):
                    await forge_poll.poll_forever()

        asyncio.run(run())
        assert calls["n"] == 3


async def _noop_sleep(_seconds):
    return None


def test_state_json_is_stored_as_json(db_path):
    """The column is read by more than this module (a status page, a human with
    sqlite3), so it must hold plain JSON rather than a repr."""
    _task, mr = _setup_mr(db_path)
    mergerequests.save_forge_state(mr["id"], {"state": "open"})
    from core.db import get_conn
    conn = get_conn()
    raw = conn.execute("SELECT forge_state FROM merge_requests WHERE id = ?",
                       (mr["id"],)).fetchone()[0]
    conn.close()
    assert json.loads(raw) == {"state": "open"}
