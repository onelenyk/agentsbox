"""Per-repo setup hook — put into a fresh worktree the files git does not carry.

A worker never works in the human's checkout: it gets a clone, and a clone
contains exactly what git tracks. Everything gitignored is simply absent —
`local.properties`, `.env`, a service-account json, a signing keystore. The
build that needs one then fails in a way that reads like a code problem, and an
agent burns a run trying to fix code that was never broken.

`worker_env` in config.yaml already solved the environment-variable half of this
(that is how JAVA_HOME/ANDROID_HOME reached workers, after a live Android task
reported "Unable to locate a Java Runtime" with Java 21 installed). This is the
file half, recorded as an open gap in docs/known-gaps.md.

Configure per repo in config.yaml, alongside `check` (see core.verify):

    repos:
      - name: android-driver
        source: /Users/lenyk/workproject/android-driver
        setup:
          copy:
            - local.properties           # <source>/local.properties → <worktree>/local.properties
            - from: ~/secrets/app.env    # an absolute source needs an explicit destination
              to: app/.env
          run: ./scripts/bootstrap-worktree.sh    # after the copies, cwd = the worktree

Both keys are optional and BOTH shapes are supported, because they answer
different questions and neither subsumes the other: `copy` is the whole answer
for a secret that only exists on this machine (a command cannot conjure one),
and `run` is the whole answer for work that has to be *derived* in the worktree
(link a node_modules cache, decrypt a bundle, seed a fixture db). A repo with no
`setup:` block is untouched and nothing runs.

Relative `from` paths are read out of the repo's own `source` directory — the
human's real checkout — because that is where the gitignored file actually
lives, and it makes the common case a single word.

FAILURE POLICY: a configured step that does not complete raises, which
core.orchestrator._spawn_worker_safe turns into "<role> failed to start: …" on
the task thread, and no agent is launched. Warning and continuing was rejected:
the worktree would then be missing exactly the file the human declared it needs,
and the damage surfaces many minutes and several model-dollars later as a
mystifying build error inside the agent's run — the same reasoning that makes
spawn_worker refuse to run at all when it cannot prepare an isolated worktree.
A loud stop at second zero is strictly cheaper to diagnose.
"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300     # 5 minutes; a setup step slower than this is a build, not setup
OUTPUT_TAIL = 4000        # what a failure message carries — the end is where the error is


class SetupHookError(RuntimeError):
    """A repo's setup hook is malformed, or one of its steps failed.

    Raised rather than returned so no caller can accidentally treat a
    half-prepared worktree as a prepared one.
    """


def _no_hook(repo_name: str) -> dict:
    return {"ran": False, "copied": [], "command": "", "exit_code": None,
            "output": "", "duration_ms": 0,
            "reason": f"repo {repo_name!r} configures no setup hook"}


def apply_setup(repo_name: Optional[str], worktree_path: str,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run *repo_name*'s setup hook against a freshly created *worktree_path*.

    Returns {ran, copied, command, exit_code, output, duration_ms, reason}.
    ``ran`` is False only when the repo configures no hook at all — reported as
    its own answer, never as a success, so "nothing was configured" and "the
    setup succeeded" stay distinguishable at every call site.

    Raises SetupHookError if the hook is configured but did not fully complete.
    """
    from core.repos import get_repo, repo_setup

    if not repo_name:
        return _no_hook("")
    raw = repo_setup(repo_name)
    if raw is None:
        return _no_hook(repo_name)
    if not isinstance(raw, dict):
        # A `setup:` that is a bare string or list is a typo, not a hook. Doing
        # nothing here would leave the worker missing files the human believes
        # they configured — the exact silence this module exists to remove.
        raise SetupHookError(
            f"repo {repo_name!r}: `setup:` must be a mapping with `copy:` "
            f"and/or `run:` keys, got {type(raw).__name__}")

    unknown = set(raw) - {"copy", "run"}
    if unknown:
        raise SetupHookError(
            f"repo {repo_name!r}: unknown key(s) in `setup:`: "
            f"{', '.join(sorted(unknown))} (expected `copy` and/or `run`)")

    raw_copy = raw.get("copy") or []
    command = str(raw.get("run") or "").strip()
    if not isinstance(raw_copy, list):
        raise SetupHookError(
            f"repo {repo_name!r}: `setup.copy` must be a list, got {type(raw_copy).__name__}")
    if not raw_copy and not command:
        # An empty hook is a half-written one. Silently succeeding would tell
        # the human it worked.
        raise SetupHookError(
            f"repo {repo_name!r}: `setup:` is present but empty — give it `copy:` or `run:`, "
            f"or remove it")

    started = time.monotonic()
    source = (get_repo(repo_name) or {}).get("source") or ""
    plan = _plan_copies(repo_name, raw_copy, source, worktree_path)

    copied, copied_files = [], []
    for src, dest in plan:
        was_file = src.is_file()
        try:
            _copy(src, dest)
        except OSError as e:
            raise SetupHookError(
                f"repo {repo_name!r}: setup hook could not copy {src} into the "
                f"worktree: {e}") from e
        rel = str(dest.relative_to(Path(worktree_path).resolve()))
        copied.append(rel)
        if was_file:
            copied_files.append(rel)

    warning = _exclude_from_commits(worktree_path, copied_files)

    exit_code, output = None, ""
    if command:
        exit_code, output = _run(repo_name, command, worktree_path, timeout)

    res = {"ran": True, "copied": copied, "command": command,
           "exit_code": exit_code, "output": output, "warning": warning,
           "duration_ms": int((time.monotonic() - started) * 1000), "reason": ""}
    if warning:
        logger.warning("setup hook for %s: %s", repo_name, warning)
    logger.info("setup hook for %s: %s", repo_name, summarise(res))
    return res


def _plan_copies(repo_name: str, entries: list, source: str, worktree_path: str):
    """Resolve every configured copy to (absolute source, absolute destination).

    Resolved fully before anything is copied, so a bad entry stops the hook
    before it has written half of it into the worktree.

    These paths come from the human's config.yaml, not from an agent, so this is
    not a hostile-input boundary — but a mistyped `to:` should not be able to
    write outside the worktree, and `..`/symlink escapes are far likelier to be
    accidents than intent. Both directions are therefore confined: destinations
    to the worktree, and *relative* sources to the repo's own source tree (spell
    an out-of-tree source as an absolute `from:` and it is allowed, because then
    it is unmistakably what was meant).
    """
    root = Path(source).expanduser().resolve() if source else None
    wt = Path(worktree_path).resolve()
    plan = []

    for entry in entries:
        if isinstance(entry, str):
            src_raw, dest_raw = entry, entry
        elif isinstance(entry, dict):
            extra = set(entry) - {"from", "to"}
            if extra:
                raise SetupHookError(
                    f"repo {repo_name!r}: unknown key(s) in a `setup.copy` entry: "
                    f"{', '.join(sorted(extra))} (expected `from` and `to`)")
            src_raw = str(entry.get("from") or "").strip()
            dest_raw = str(entry.get("to") or "").strip() or src_raw
        else:
            raise SetupHookError(
                f"repo {repo_name!r}: each `setup.copy` entry must be a path or a "
                f"{{from, to}} mapping, got {type(entry).__name__}")

        if not src_raw:
            raise SetupHookError(f"repo {repo_name!r}: a `setup.copy` entry has no `from`")

        src_path = Path(src_raw).expanduser()
        if src_path.is_absolute():
            src = src_path.resolve()
            if dest_raw == src_raw:
                # "~/secrets/app.env" says nothing about where it belongs in the
                # worktree, and guessing (basename? full path?) would be a
                # silent guess about a credentials file.
                raise SetupHookError(
                    f"repo {repo_name!r}: `setup.copy` source {src_raw!r} is absolute, "
                    f"so it needs an explicit destination: "
                    f"- from: {src_raw}\n  to: <path inside the repo>")
        else:
            if root is None:
                raise SetupHookError(
                    f"repo {repo_name!r}: `setup.copy` entry {src_raw!r} is relative but "
                    f"the repo has no `source:` to read it from")
            src = (root / src_path).resolve()
            if not _within(src, root):
                raise SetupHookError(
                    f"repo {repo_name!r}: `setup.copy` source {src_raw!r} resolves to {src}, "
                    f"outside the repo source dir {root} — use an absolute `from:` if that "
                    f"is really what you want")

        if not src.exists():
            raise SetupHookError(
                f"repo {repo_name!r}: `setup.copy` source {src} does not exist — "
                f"the worker would have started without it")

        dest_path = Path(dest_raw)
        if dest_path.is_absolute() or dest_raw.startswith("~"):
            raise SetupHookError(
                f"repo {repo_name!r}: `setup.copy` destination {dest_raw!r} must be relative "
                f"to the worktree")
        dest = (wt / dest_path).resolve()
        if not _within(dest, wt) or dest == wt:
            raise SetupHookError(
                f"repo {repo_name!r}: `setup.copy` destination {dest_raw!r} resolves to {dest}, "
                f"outside the worktree {wt}")

        plan.append((src, dest))
    return plan


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        # copy2, so a 600 credentials file arrives still 600. This is also why
        # the hook runs AFTER core.worktree.set_access: set_access rewrites every
        # mode in the tree to 644/755, which would widen a copied secret.
        shutil.copy2(src, dest)


def _exclude_from_commits(worktree_path: str, rel_paths: list) -> str:
    """Keep copied files out of the agent's commits. Returns a warning, or "".

    core.git_ops.commit_all stages with ``git add -A -- .``, and
    core.worker.cleanup_task's salvage does ``git add -A -N`` + diff into a
    stored patch. A file this hook drops somewhere the repo does not already
    ignore would therefore ride into a commit — and a copied `.env` into a
    merge request. These files exist precisely because git does not carry them;
    handing them to a worker must not start carrying them.

    Written to the worktree's own ``info/exclude``, never a tracked .gitignore,
    so nothing about the repo's history or contents changes. Paths the repo's
    own .gitignore already covers (the common case — `local.properties` in an
    Android repo) are left alone.

    Only file destinations. A copied *directory* is deliberately not excluded:
    an anchored ignore on a directory would also hide new untracked files the
    agent legitimately creates inside it, and silently dropping the agent's own
    work from its commit is worse than the leak this guards against.

    Failing to exclude is a warning, not a raise: unlike a missing file, it
    leaves the worktree exactly as configured — a safety net is absent, not a
    requested file. It is logged, and carried into the thread line by summarise.
    """
    wt = Path(worktree_path)
    if not rel_paths or not (wt / ".git").exists():
        return ""      # repo-less scratch dir / plain dir: nothing git can commit
    common = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, timeout=30)
    if common.returncode != 0:
        return (f"copied files could not be excluded from commits "
                f"({common.stderr.strip()}) — check them before merging")
    gitdir = Path(common.stdout.strip())
    if not gitdir.is_absolute():
        gitdir = wt / gitdir

    wanted = []
    for rel in rel_paths:
        already = subprocess.run(["git", "-C", str(wt), "check-ignore", "-q", rel],
                                 capture_output=True, timeout=30)
        if already.returncode != 0:
            wanted.append("/" + rel.replace(os.sep, "/"))
    if not wanted:
        return ""

    exclude = gitdir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        lines = current.splitlines()
        new = [p for p in wanted if p not in lines]
        if new:
            prefix = "" if (not current or current.endswith("\n")) else "\n"
            exclude.write_text(current + prefix
                               + "# added by Sisyphus's per-repo setup hook\n"
                               + "\n".join(new) + "\n")
    except OSError as e:
        return (f"copied files could not be excluded from commits ({e}) — "
                f"check them before merging")
    return ""


def _run(repo_name: str, command: str, worktree_path: str, timeout: int):
    """Run the hook's command in the worktree. Returns (exit_code, output)."""
    env = dict(os.environ)
    from core.config import worker_env
    env.update(worker_env() or {})
    from core.runner_detect import path_with_install_dirs
    env["PATH"] = path_with_install_dirs(env.get("PATH", ""))
    # Setting up a worktree has no business reaching the daemon's own database
    # (same stance as core.verify's check runs).
    from .db import DB_PATH as _daemon_db
    env["SISYPHUS_DB_PROTECT"] = _daemon_db

    try:
        proc = subprocess.run(command, shell=True, cwd=worktree_path, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SetupHookError(
            f"repo {repo_name!r}: setup command exceeded {timeout}s and was killed: "
            f"{command}") from e
    except OSError as e:
        raise SetupHookError(
            f"repo {repo_name!r}: setup command could not be started: {command} ({e})") from e

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()[-OUTPUT_TAIL:]
    if proc.returncode != 0:
        raise SetupHookError(
            f"repo {repo_name!r}: setup command failed (exit {proc.returncode}): "
            f"{command}\n{out}")
    return proc.returncode, out


def summarise(res: dict) -> str:
    """One line for the task thread, phrased so a hook that never ran cannot be
    misread as one that prepared the worktree."""
    if not res.get("ran"):
        return f"No repo setup hook ran — {res.get('reason', 'not configured')}."
    parts = []
    copied = res.get("copied") or []
    if copied:
        parts.append(f"copied {len(copied)} path(s) into the worktree: " + ", ".join(copied))
    if res.get("command"):
        parts.append(f"ran `{res['command']}` (exit {res.get('exit_code')})")
    line = "Repo setup hook: " + "; ".join(parts) + "."
    if res.get("warning"):
        line += " WARNING: " + res["warning"]
    return line
