"""core.health — repo health runs (build · tests · coverage) and their discovery."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import health, store


JUNIT = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="a" tests="4">
    <testcase classname="app.LoginTest" name="accepts"/>
    <testcase classname="app.LoginTest" name="rejects"><failure message="boom"/></testcase>
    <testcase classname="app.MapTest" name="crashes"><error message="npe"/></testcase>
    <testcase classname="app.MapTest" name="later"><skipped/></testcase>
  </testsuite>
</testsuites>"""

JACOCO = """<?xml version="1.0"?>
<report name="app"><package name="p"><counter type="LINE" missed="99" covered="1"/></package>
<counter type="INSTRUCTION" missed="10" covered="10"/><counter type="LINE" missed="25" covered="75"/></report>"""

COBERTURA = '<?xml version="1.0"?><coverage line-rate="0.614" branch-rate="0.5"></coverage>'


# ── parsers ──────────────────────────────────────────────────────────────────

def test_junit_counts_cases_and_names_the_failures(tmp_path):
    (tmp_path / "a.xml").write_text(JUNIT)
    (tmp_path / "b.xml").write_text('<testsuite><testcase name="solo"/></testsuite>')
    (tmp_path / "broken.xml").write_text("<not xml")
    r = health.parse_junit([str(tmp_path / n) for n in ("a.xml", "b.xml", "broken.xml")])
    assert (r["total"], r["failed"], r["skipped"], r["passed"]) == (5, 2, 1, 2)
    assert r["failed_names"] == ["app.LoginTest.rejects", "app.MapTest.crashes"]


def test_junit_with_nothing_parsed_is_unknown_not_zero(tmp_path):
    assert health.parse_junit([]) is None
    (tmp_path / "x.xml").write_text("garbage")
    assert health.parse_junit([str(tmp_path / "x.xml")]) is None


def test_coverage_reads_the_report_level_line_counter_and_cobertura(tmp_path):
    (tmp_path / "jacoco.xml").write_text(JACOCO)
    (tmp_path / "cov.xml").write_text(COBERTURA)
    assert health.parse_coverage(str(tmp_path / "jacoco.xml")) == 75.0   # not the package's 1%
    assert health.parse_coverage(str(tmp_path / "cov.xml")) == 61.4
    assert health.parse_coverage(str(tmp_path / "missing.xml")) is None


def test_clean_profile_keeps_known_keys_only():
    assert health.clean_profile({"build": " make ", "test": "", "junit": "a/*.xml",
                                 "timeout": "60", "rm": "-rf /"}) == \
        {"build": "make", "junit": "a/*.xml", "timeout": 60}


# ── a real run against a throwaway git repo ──────────────────────────────────

def _git_repo(tmp_path, files: dict):
    src = tmp_path / "proj"
    src.mkdir()
    for name, body in files.items():
        (src / name).write_text(body)
    run = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return src


def _with_repo(src, prof):
    repo = {"name": "p", "source": str(src), "branch": "main", "health": prof}
    return patch("core.repos.load_repos", return_value=[repo])


def test_a_run_reports_counts_failures_and_coverage(db_path, tmp_path):
    src = _git_repo(tmp_path, {
        "junit.xml": JUNIT,
        "cov.xml": COBERTURA,
        # the "test command" copies the reports where a tool would write them
        "t.sh": "mkdir -p out && cp junit.xml out/ && cp cov.xml out/ && exit 1\n",
    })
    prof = {"build": "echo compiling", "test": "sh t.sh", "junit": "out/*.xml",
            "coverage_report": "out/cov.xml"}
    with _with_repo(src, prof):
        r = health.run("p")
    assert r["status"] == "fail"
    assert [s["name"] for s in r["stages"]] == ["build", "test"]
    assert r["stages"][0]["status"] == "ok"
    t = r["stages"][1]
    assert t["status"] == "fail" and "1 passed · 2 failed · 1 skipped" in t["detail"]
    assert "app.MapTest.crashes" in t["detail"]
    assert (r["tests_total"], r["tests_failed"]) == (4, 2)
    assert r["coverage_pct"] == 61.4
    assert len(r["head"]) == 40
    assert health.last_run("p")["id"] == r["id"]            # recorded
    assert r["summary"] == "builds · 2 of 4 tests fail · 61.4% coverage"


def test_a_broken_build_skips_the_tests_and_shows_the_error(db_path, tmp_path):
    src = _git_repo(tmp_path, {"README": "x"})
    prof = {"build": "echo 'e: Main.kt:3 Unresolved reference: foo' >&2; exit 2", "test": "true"}
    with _with_repo(src, prof):
        r = health.run("p")
    assert r["status"] == "fail"
    b, t = r["stages"]
    assert b["status"] == "fail" and "Unresolved reference" in b["detail"]
    assert t["status"] == "skipped"
    assert r["summary"] == "build broken"


def test_green_tests_without_junit_say_the_counts_are_unknown(db_path, tmp_path):
    src = _git_repo(tmp_path, {"README": "x"})
    with _with_repo(src, {"test": "true"}):
        r = health.run("p")
    assert r["status"] == "ok"
    assert "counts unknown" in r["stages"][0]["detail"]
    assert r["tests_total"] is None


def test_the_run_uses_a_fresh_clone_with_the_setup_hook(db_path, tmp_path):
    """A gitignored file the build needs arrives through the setup hook, and the
    human's checkout is never where the command runs."""
    src = _git_repo(tmp_path, {".gitignore": "local.properties\n"})
    (src / "local.properties").write_text("sdk.dir=/x")
    repo = {"name": "p", "source": str(src), "branch": "main",
            "setup": {"copy": ["local.properties"]},
            "health": {"build": "test -f local.properties && touch built-here"}}
    with patch("core.repos.load_repos", return_value=[repo]):
        r = health.run("p")
    assert r["status"] == "ok", r["stages"]
    assert not (src / "built-here").exists()


def test_no_profile_or_unknown_branch_is_unknown_not_red(db_path, tmp_path):
    src = _git_repo(tmp_path, {"README": "x"})
    with _with_repo(src, {}):
        assert health.run("p", record=False)["status"] == "unknown"
    repo = {"name": "p", "source": str(src), "branch": "nope", "health": {"test": "true"}}
    with patch("core.repos.load_repos", return_value=[repo]):
        r = health.run("p", record=False)
    assert r["status"] == "unknown" and r["stages"][0]["name"] == "checkout"


# ── schedule + concurrency ───────────────────────────────────────────────────

def test_due_only_after_a_first_run_and_once_the_interval_passed(db_path):
    repos = [{"name": "a", "source": "/x", "health": {"test": "true"}},
             {"name": "never", "source": "/x", "health": {"test": "true"}},
             {"name": "noprofile", "source": "/x"}]
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    health.record_run({"repo": "a", "status": "ok", "ran_at": old})
    health.record_run({"repo": "noprofile", "status": "ok", "ran_at": old})
    with patch("core.repos.load_repos", return_value=repos):
        assert health.due() == ["a"]
        health.record_run({"repo": "a", "status": "ok"})          # fresh run
        assert health.due() == []


def test_only_one_run_at_a_time():
    assert health._lock.acquire(blocking=False)
    try:
        assert health.start("anything") is False
    finally:
        health._lock.release()


# ── discovery: the profiler agent and its tool ───────────────────────────────

def test_profiler_is_a_seeded_pinned_worker_with_read_only_tools(db_path):
    from core import agents
    tools = agents.agent_tools(health.PROFILER_AGENT)
    assert set(tools) == {"get_context", "comment", "propose_health_profile", "finish"}


def test_propose_health_profile_stores_a_proposal_and_never_touches_config(db_path):
    from web.app import app
    from core import sessions
    with patch("core.orchestrator.emit") as emit:
        task = health.start_profiling("kmp")
    emit.assert_called_once_with(task["id"], "created")      # the dispatcher hears of it
    assert task["agent"] == "repo-profiler" and task["repo"] == "kmp"
    token = sessions.mint(task["id"], "repo-profiler")
    c = TestClient(app)

    def call(args):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "propose_health_profile", "arguments": args}}
        return c.post("/mcp/agent", json=body,
                      headers={"Authorization": f"Bearer {token}"}).json()["result"]

    with patch("core.repos.update_repo_config") as write:
        assert call({"notes": "nothing to run"})["isError"] is True
        out = call({"build": "make build", "test": "make test", "junit": "**/*.xml",
                    "notes": "read docs/agent/build-and-run.md"})
    assert out["isError"] is False
    write.assert_not_called()
    p = health.get_proposal("kmp")
    assert p["profile"] == {"build": "make build", "test": "make test", "junit": "**/*.xml"}
    assert p["task_id"] == task["id"]
    assert "make test" in store.get_thread(task["id"])[-1]["text"]


# ── the card ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path):
    from web.app import app
    return TestClient(app)


def test_card_states(client, db_path):
    base = {"name": "p", "source": "/x", "branch": "main"}
    with patch("core.repos.load_repos", return_value=[dict(base)]):
        assert "Set up health" in client.get("/api/repos/p/health").text
        health.save_proposal("p", {"test": "make test"}, notes="from the Makefile")
        html = client.get("/api/repos/p/health").text
        assert "proposed" in html and 'value="make test"' in html and "from the Makefile" in html

    with patch("core.repos.load_repos", return_value=[dict(base, health={"test": "make test"})]):
        health.clear_proposal("p")
        assert "never run" in client.get("/api/repos/p/health").text
        health.record_run({"repo": "p", "status": "fail", "summary": "build broken",
                           "stages": [{"name": "build", "status": "fail", "detail": "e: boom",
                                       "output": "…e: boom", "command": "make", "secs": 3}]})
        html = client.get("/api/repos/p/health").text
        assert "broken" in html and "e: boom" in html


def test_save_writes_the_confirmed_profile_and_clears_the_proposal(client, db_path):
    health.save_proposal("p", {"test": "x"})
    with patch("core.repos.load_repos", return_value=[{"name": "p", "source": "/x"}]), \
         patch("core.repos.update_repo_config") as write:
        r = client.post("/api/repos/p/health/save", data={"build": "make build", "test": "make test",
                                                          "timeout": "600"})
        assert r.status_code == 200
        client.post("/api/repos/p/health/save", data={"build": " ", "test": ""})   # refused
    write.assert_called_once_with("p", health={"build": "make build", "test": "make test",
                                               "timeout": 600})
    assert health.get_proposal("p") is None


def test_adding_a_repo_starts_the_profiler(client, db_path, tmp_path):
    src = _git_repo(tmp_path, {"README": "x"})
    with patch("core.repos._save_repo_config"), \
         patch("core.repos.load_repos", return_value=[{"name": "n", "source": str(src)}]):
        r = client.post("/api/repos", data={"name": "n", "source": str(src)})
    assert r.status_code == 200
    t = health.profiling_task("n")
    assert t and t["agent"] == "repo-profiler"


def test_update_repo_config_writes_and_clears_health():
    import core.repos
    repos = [{"name": "r", "source": "/on/disk", "check": "pytest"}]
    for value, expect in (({"test": "make test", "junk": 1}, {"test": "make test"}), ({}, None)):
        cfg = {"repos": [dict(r) for r in repos]}
        with patch("core.repos.yaml.safe_load", return_value=cfg), \
             patch("core.repos.yaml.dump", return_value="") as dump, \
             patch("core.config.reload_config"), \
             patch.object(core.repos.Path, "read_text", return_value=""), \
             patch.object(core.repos.Path, "exists", return_value=True), \
             patch.object(core.repos.Path, "write_text"):
            core.repos.update_repo_config("r", health=value)
        saved = dump.call_args[0][0]["repos"][0]
        assert saved.get("health") == expect and saved["check"] == "pytest"
        json.dumps(saved)
