"""Tests for the `worker_env` config block.

The daemon is normally started by launchd, whose environment is nearly empty:
PATH=/usr/bin:/bin:/usr/sbin:/sbin, no JAVA_HOME, no ANDROID_HOME. Workers
inherit it, so a real Android task reported "Unable to locate a Java Runtime"
with Java 21 installed and never executed a line of the code it wrote. This
block is how a deployment supplies the toolchain without putting it in git.
"""
from unittest.mock import patch

from core import config


def _with_config(block, base=None):
    with patch("core.config.get_config", return_value=block):
        return config.worker_env(base=base or {})


class TestWorkerEnv:
    def test_absent_block_yields_nothing(self):
        with patch("core.config.get_config", return_value={}):
            assert config.worker_env(base={}) == {}

    def test_plain_values_pass_through(self):
        out = _with_config({"JAVA_HOME": "/opt/jdk", "ANDROID_HOME": "/opt/sdk"})
        assert out == {"JAVA_HOME": "/opt/jdk", "ANDROID_HOME": "/opt/sdk"}

    def test_tilde_is_expanded(self):
        out = _with_config({"ANDROID_HOME": "~/Library/Android/sdk"})
        assert not out["ANDROID_HOME"].startswith("~")
        assert out["ANDROID_HOME"].endswith("/Library/Android/sdk")

    def test_a_later_key_can_build_on_an_earlier_one(self):
        """PATH: "${JAVA_HOME}/bin:${PATH}" is the whole point — appending to the
        daemon's PATH rather than replacing it and losing /usr/bin."""
        out = _with_config(
            {"JAVA_HOME": "/opt/jdk", "PATH": "${JAVA_HOME}/bin:${PATH}"},
            base={"PATH": "/usr/bin:/bin"},
        )
        assert out["PATH"] == "/opt/jdk/bin:/usr/bin:/bin"

    def test_reserved_names_are_refused(self):
        """WORKSPACE_ROOT is what keeps a worker's tool calls and its test runs
        off the live daemon database. Config must not be able to take it back."""
        block = {"WORKSPACE_ROOT": "/tmp/hijack", "SISYPHUS_DB_READONLY": "0",
                 "JAVA_HOME": "/opt/jdk"}
        out = _with_config(block)
        assert out == {"JAVA_HOME": "/opt/jdk"}
        with patch("core.config.get_config", return_value=block):
            assert set(config.worker_env_rejected()) == {"WORKSPACE_ROOT",
                                                         "SISYPHUS_DB_READONLY"}

    def test_a_malformed_block_does_not_explode(self):
        with patch("core.config.get_config", return_value="not-a-mapping"):
            assert config.worker_env(base={}) == {}
            assert config.worker_env_rejected() == []


class TestDbWriteProtect:
    """SISYPHUS_DB_PROTECT guards exactly one file — the daemon's database —
    instead of making every SQLite connection read-only. The blanket flag made
    a worker's own test run fail with ~540 'unable to open database' errors
    on databases the daemon has never seen."""

    def _fresh_db(self, tmp_path, monkeypatch, name):
        import core.db as db
        p = tmp_path / name
        monkeypatch.setattr(db, "DB_PATH", str(p))
        return db, str(p)

    def test_other_databases_stay_writable_under_protect(self, tmp_path, monkeypatch):
        db, mine = self._fresh_db(tmp_path, monkeypatch, "suite-tmp.db")
        monkeypatch.setenv("SISYPHUS_DB_PROTECT", str(tmp_path / "daemon.db"))
        monkeypatch.delenv("SISYPHUS_DB_READONLY", raising=False)
        conn = db.get_conn()
        conn.execute("CREATE TABLE t (x)")     # would raise on a ro connection
        conn.close()

    def test_the_protected_database_is_read_only(self, tmp_path, monkeypatch):
        import sqlite3
        import pytest as _pytest
        db, mine = self._fresh_db(tmp_path, monkeypatch, "daemon.db")
        sqlite3.connect(mine).close()          # the file must exist for mode=ro
        monkeypatch.setenv("SISYPHUS_DB_PROTECT", mine)
        monkeypatch.delenv("SISYPHUS_DB_READONLY", raising=False)
        conn = db.get_conn()
        with _pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")
        conn.close()

    def test_blanket_readonly_still_blankets(self, tmp_path, monkeypatch):
        import sqlite3
        import pytest as _pytest
        db, mine = self._fresh_db(tmp_path, monkeypatch, "any.db")
        sqlite3.connect(mine).close()
        monkeypatch.setenv("SISYPHUS_DB_READONLY", "1")
        monkeypatch.delenv("SISYPHUS_DB_PROTECT", raising=False)
        conn = db.get_conn()
        with _pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")
        conn.close()

    def test_the_check_hands_out_the_daemon_path(self, tmp_path, monkeypatch, db_path):
        """verify.run_check passes PROTECT=<daemon db>, not the blanket flag,
        so the suite it runs can write its own tmp databases."""
        import subprocess as sp
        import core.db as db
        from core import verify
        seen = {}
        real_run = sp.run

        def capture(cmd, **kw):
            if kw.get("shell"):
                seen.update(kw.get("env") or {})
                return real_run("true", shell=True, capture_output=True, text=True)
            return real_run(cmd, **kw)

        # a real bare repo so the throwaway worktree can be created
        bare = tmp_path / "bare"; wt = tmp_path / "seed"; wt.mkdir()
        real_run(["git", "init", "-b", "main", str(wt)], capture_output=True)
        real_run(["git", "-C", str(wt), "config", "user.email", "t@t"], capture_output=True)
        real_run(["git", "-C", str(wt), "config", "user.name", "t"], capture_output=True)
        (wt / "f").write_text("x")
        real_run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
        real_run(["git", "-C", str(wt), "commit", "-m", "i"], capture_output=True)
        real_run(["git", "clone", "--bare", str(wt), str(bare)], capture_output=True)

        monkeypatch.setattr(verify, "check_command", lambda repo: "true")
        monkeypatch.setattr(verify.subprocess, "run", capture)
        res = verify.run_check({"repo": "r", "bare_path": str(bare), "source_branch": "main"})
        assert res["ran"] is True
        assert seen.get("SISYPHUS_DB_PROTECT") == db.DB_PATH
        assert "SISYPHUS_DB_READONLY" not in seen
