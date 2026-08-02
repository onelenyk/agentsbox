"""Agent-context resource scanning (core/repo_context.py)."""
from core import repo_context


def test_resources_finds_and_orders(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * 10)
    (tmp_path / "README.md").write_text("y")
    d = tmp_path / ".opencode"
    d.mkdir()
    (d / "config.json").write_text("{}")

    res = repo_context.resources(str(tmp_path))
    by = {r["path"]: r for r in res}
    assert by["AGENTS.md"]["kind"] == "file" and by["AGENTS.md"]["size"] == 10
    assert by["README.md"]["kind"] == "file"
    assert by[".opencode"]["kind"] == "dir" and by[".opencode"]["count"] == 1
    order = [r["path"] for r in res]
    assert order.index("AGENTS.md") < order.index("README.md")  # priority order


def test_read_resource_and_traversal_guard(tmp_path):
    (tmp_path / "AGENTS.md").write_text("hello")
    assert repo_context.read_resource(str(tmp_path), "AGENTS.md") == "hello"
    assert repo_context.read_resource(str(tmp_path), "../../../etc/passwd") is None
    assert repo_context.read_resource(str(tmp_path), "missing.md") is None


def test_summary_prunes_and_counts(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")
    (tmp_path / "c.md").write_text("z")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "junk.js").write_text("skip me")

    s = repo_context.summary(str(tmp_path))
    assert s["exists"] and s["files"] == 3  # node_modules pruned
    assert dict(s["langs"]).get(".py") == 2


def test_missing_source(tmp_path):
    assert repo_context.resources(str(tmp_path / "nope")) == []
    assert repo_context.summary(str(tmp_path / "nope")) == {"exists": False}
