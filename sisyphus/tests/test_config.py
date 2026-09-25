"""Unit tests for core.config — load_config, get_config, reload_config."""

from unittest.mock import patch

import core.config


def _make_fake_path(tmp_path):
    """Return a FakePath class that resolves Path(__file__).parent.parent / yaml
    to tmp_path / yaml (a real pathlib.Path, so .exists() / .read_text() work).
    """

    class FakePath:
        def __init__(self, *args, **kwargs):
            pass  # ignore __file__

        @property
        def parent(self):
            return self  # .parent.parent = self

        def __truediv__(self, other):
            return tmp_path / other  # real pathlib.Path → .exists() works

    return FakePath


def _write_config(tmp_path, content: str):
    """Write content to tmp_path/config.yaml."""
    (tmp_path / "config.yaml").write_text(content)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------
class TestLoadConfig:
    """Tests for load_config() — file loading, env-var substitution, cache."""

    def test_valid_config_returns_parsed_dict(self, tmp_path):
        """load_config reads a valid yaml and returns nested dict."""
        _write_config(
            tmp_path,
            "openrouter:\n  base_url: https://api.openrouter.ai\n  api_key: sk-test\n",
        )

        core.config._config = None  # reset singleton
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.load_config()

        assert result == {
            "openrouter": {"base_url": "https://api.openrouter.ai", "api_key": "sk-test"}
        }

    def test_missing_config_returns_empty_dict(self, tmp_path):
        """load_config returns {} when config.yaml is absent."""
        # Ensure no config.yaml exists in tmp_path
        core.config._config = None

        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.load_config()

        assert result == {}

    def test_env_var_substitution(self, tmp_path, monkeypatch):
        """A plain ${ENV_VAR} placeholder is substituted from os.environ."""
        monkeypatch.setenv("TEST_LOG_LEVEL", "debug")
        _write_config(tmp_path, "log_level: ${TEST_LOG_LEVEL}\n")

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.load_config()

        assert result == {"log_level": "debug"}

    def test_credential_placeholders_come_from_the_vault_not_the_environment(self, tmp_path, monkeypatch):
        """A credential line resolves from the vault only; the environment is ignored."""
        from core import credentials
        monkeypatch.setenv("TEST_API_KEY", "sk-from-env")
        monkeypatch.setitem(credentials._cache, "TEST_BASE_URL", "https://substituted.example.com")
        monkeypatch.setitem(credentials._cache, "TEST_API_KEY", None)
        _write_config(
            tmp_path,
            "openrouter:\n  base_url: ${TEST_BASE_URL}\n  api_key: ${TEST_API_KEY}\n",
        )

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.load_config()

        assert result == {
            "openrouter": {
                "base_url": "https://substituted.example.com",
                "api_key": "${TEST_API_KEY}",
            }
        }

    def test_missing_env_var_placeholder_left_as_is(self, tmp_path):
        """${MISSING} with no env var stays as literal '${MISSING}'."""
        _write_config(tmp_path, "endpoint: ${MISSING_VAR}\n")

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.load_config()

        assert result == {"endpoint": "${MISSING_VAR}"}

    def test_singleton_cache_returns_cached(self, tmp_path):
        """Second call to load_config returns cached config without re-reading."""
        _write_config(tmp_path, "value: first\n")

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            first = core.config.load_config()

        # Change file on disk but don't reset cache
        _write_config(tmp_path, "value: second\n")

        with patch("core.config.Path", _make_fake_path(tmp_path)):
            second = core.config.load_config()

        assert second == first == {"value": "first"}


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------
class TestGetConfig:
    """Tests for get_config() — key traversal, dot-notation, defaults.

    These tests bypass file reading by setting core.config._config directly.
    """

    def setup_method(self):
        """Reset the singleton cache before each test."""
        core.config._config = None

    def test_top_level_key(self):
        """get_config('key') returns top-level value."""
        core.config._config = {"host": "0.0.0.0", "port": 7677}
        assert core.config.get_config("host") == "0.0.0.0"
        assert core.config.get_config("port") == 7677

    def test_dot_notation_nested_key(self):
        """get_config('a.b.c') traverses nested dicts."""
        core.config._config = {
            "openrouter": {"base_url": "https://api.openrouter.ai", "api_key": "sk-test"}
        }
        assert core.config.get_config("openrouter.base_url") == "https://api.openrouter.ai"
        assert core.config.get_config("openrouter.api_key") == "sk-test"

    def test_missing_key_returns_default(self):
        """Missing key returns the caller-supplied default."""
        core.config._config = {"host": "0.0.0.0"}
        assert core.config.get_config("port", default=7677) == 7677

    def test_missing_key_no_default_returns_none(self):
        """Missing key with no default → None."""
        core.config._config = {"host": "0.0.0.0"}
        assert core.config.get_config("port") is None

    def test_no_key_returns_entire_config(self):
        """get_config() without arguments returns the full config dict."""
        cfg = {"host": "0.0.0.0", "port": 7677}
        core.config._config = cfg
        assert core.config.get_config() is cfg
        assert core.config.get_config(key=None) is cfg

    def test_partial_path_returns_default(self):
        """Non-dict encountered during traversal → return default."""
        core.config._config = {"a": {"b": 42}}
        # "a.b.c" → at "b" value is 42 (not a dict), so returns default
        assert core.config.get_config("a.b.c", default="fallback") == "fallback"

    def test_none_value_returns_default(self):
        """If the resolved value is None, get_config returns the default."""
        core.config._config = {"optional": None}
        assert core.config.get_config("optional", default="default-val") == "default-val"


# ---------------------------------------------------------------------------
# reload_config
# ---------------------------------------------------------------------------
class TestReloadConfig:
    """Tests for reload_config() — cache invalidation and re-read."""

    def test_reload_reads_modified_file(self, tmp_path):
        """reload_config picks up changes to config.yaml on disk."""
        _write_config(tmp_path, "version: 1\n")

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            core.config.load_config()
            assert core.config.get_config("version") == 1

        # Modify config on disk
        _write_config(tmp_path, "version: 2\n")

        with patch("core.config.Path", _make_fake_path(tmp_path)):
            core.config.reload_config()
            assert core.config.get_config("version") == 2

    def test_reload_resets_singleton_cache(self, tmp_path):
        """reload_config clears the cache so load_config reads fresh."""
        _write_config(tmp_path, "key: first\n")

        core.config._config = None
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            core.config.load_config()
            assert core.config.get_config("key") == "first"

        # Reload with no file change — still reads again (not cached)
        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.reload_config()
        assert result == {"key": "first"}

    def test_reload_after_missing_file_returns_empty(self, tmp_path):
        """reload when file disappears returns {}."""
        core.config._config = {"stale": "data"}

        with patch("core.config.Path", _make_fake_path(tmp_path)):
            result = core.config.reload_config()
        assert result == {}
        assert core.config._config == {}
