"""Zero-dependency .env loader (core/env.py)."""
import os

from core import env


def test_loads_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\nBAZ=qux\n")
    os.environ.pop("FOO", None)
    os.environ.pop("BAZ", None)
    n = env.load_env(p)
    assert n == 2
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAZ"] == "qux"


def test_does_not_override_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ALREADY=from_file\n")
    os.environ["ALREADY"] = "from_shell"
    assert env.load_env(p) == 0
    assert os.environ["ALREADY"] == "from_shell"  # shell wins


def test_ignores_comments_blanks_and_strips_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# a comment\n\nQ="quoted value"\nS=\'single\'\nEMPTY=\nNOEQ\n')
    for k in ("Q", "S", "EMPTY"):
        os.environ.pop(k, None)
    env.load_env(p)
    assert os.environ["Q"] == "quoted value"
    assert os.environ["S"] == "single"
    assert os.environ["EMPTY"] == ""


def test_missing_file_is_noop(tmp_path):
    assert env.load_env(tmp_path / "nope.env") == 0
