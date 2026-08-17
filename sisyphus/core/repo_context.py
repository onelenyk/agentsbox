"""What the AI sees in a repo — the convention/instruction files agents read as
context, plus a light codebase summary. Read-only scan of the repo's working
tree (the configured `source`), never the bare clone.
"""
import os
from pathlib import Path

# Files agents & coding tools conventionally read as instructions / rules.
KNOWN_RESOURCES = [
    ("AGENTS.md", "Agent instructions"),
    ("CLAUDE.md", "Claude instructions"),
    (".cursorrules", "Cursor rules"),
    (".cursor/rules", "Cursor rules"),
    (".github/copilot-instructions.md", "Copilot instructions"),
    (".windsurfrules", "Windsurf rules"),
    ("CONVENTIONS.md", "Conventions"),
    ("CONTRIBUTING.md", "Contributing guide"),
    ("README.md", "Readme"),
    (".sisyphus", "Sisyphus notes"),
]


def _runner_resources():
    """Runner-owned config paths, from core.runner_profiles rather than listed
    here — so a new runner's config is recognised without editing this file."""
    from core.runner_profiles import workspace_artifact_labels
    return workspace_artifact_labels()


KNOWN_RESOURCES += _runner_resources()

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", "target", ".idea", ".pytest_cache"}


def resources(source_path: str) -> list:
    """The agent-context files/dirs that exist in this repo, in priority order."""
    root = Path(source_path or "")
    out = []
    if not root.exists():
        return out
    for rel, label in KNOWN_RESOURCES:
        p = root / rel
        if not p.exists():
            continue
        if p.is_dir():
            files = []
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    files.append(str(f.relative_to(root)))
                    if len(files) >= 100:
                        break
            out.append({"path": rel, "label": label, "kind": "dir",
                        "count": len(files), "files": files})
        else:
            out.append({"path": rel, "label": label, "kind": "file",
                        "size": p.stat().st_size})
    return out


def read_resource(source_path: str, rel: str):
    """Safe read of a file inside the repo (traversal-guarded, size-capped)."""
    root = Path(source_path or "").resolve()
    try:
        p = (root / rel).resolve()
    except Exception:
        return None
    if p != root and not str(p).startswith(str(root) + os.sep):
        return None  # escape attempt
    if p.is_file() and p.stat().st_size <= 300_000:
        try:
            return p.read_text(errors="replace")
        except Exception:
            return None
    return None


def summary(source_path: str, max_files: int = 20000) -> dict:
    """Light codebase stats: file count + top file extensions (pruned walk)."""
    root = Path(source_path or "")
    if not root.exists():
        return {"exists": False}
    exts, n = {}, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            n += 1
            ext = os.path.splitext(fn)[1].lower()
            if ext:
                exts[ext] = exts.get(ext, 0) + 1
            if n >= max_files:
                break
        if n >= max_files:
            break
    top = sorted(exts.items(), key=lambda kv: -kv[1])[:6]
    return {"exists": True, "files": n, "langs": top, "capped": n >= max_files}
