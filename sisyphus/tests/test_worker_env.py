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
