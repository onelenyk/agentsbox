"""Local Merge Requests — a first-class, mergeable change for a local repo.

A forge (GitHub/GitLab) hosts merge requests for us. A local repo has none, so
Sisyphus is the forge: when an engineer opens a change on a local repo, we record
a Merge Request with a lifecycle (open → merged | closed) and real actions —
choose a target branch, check mergeability, land the branch, or merge it — that
operate on the user's actual repo, on demand and reversibly.

The engineer's work already lives on `source_branch` (sisyphus/<task_id>) in the
per-task bare clone (`bare_path`); `source_path` is the user's real repo that the
Land/Merge actions touch.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .db import get_conn

MR_STATUSES = ("open", "merged", "closed")


def _run(args):
    return subprocess.run(args, capture_output=True, text=True)


# ── Git actions on the user's real local repo (choose target / check / land) ──


def list_branches(real_repo: str) -> List[str]:
    """Branch names in the user's real repo — the choices for the merge target."""
    if not real_repo:
        return []
    r = _run(["git", "-C", real_repo, "branch", "--format=%(refname:short)"])
    return [b.strip() for b in r.stdout.splitlines() if b.strip()] if r.returncode == 0 else []


def check_mergeability(bare_path: str, real_repo: str, source: str, target: str) -> dict:
    """Dry-run merging *source* into *target*, entirely in a throwaway worktree of
    the bare clone — never touches the user's repo. Returns
    {ok, mergeable, conflicts[]} or {ok: False, error}."""
    if not (bare_path and real_repo):
        return {"ok": False, "error": "missing repo paths"}
    tmpref = f"refs/mrcheck/{target}"
    wt = tempfile.mkdtemp(prefix="sisyphus-mrcheck-")
    try:
        f = _run(["git", "-C", bare_path, "fetch", real_repo, f"{target}:{tmpref}"])
        if f.returncode != 0:
            return {"ok": False, "error": f"cannot read target '{target}': {f.stderr.strip()[:120]}"}
        _run(["git", "-C", bare_path, "worktree", "add", "--detach", wt, tmpref])
        m = _run(["git", "-C", wt, "merge", "--no-commit", "--no-ff", source])
        conflicts = []
        if m.returncode != 0:
            d = _run(["git", "-C", wt, "diff", "--name-only", "--diff-filter=U"])
            conflicts = [c for c in d.stdout.splitlines() if c.strip()]
        _run(["git", "-C", wt, "merge", "--abort"])
        return {"ok": True, "mergeable": m.returncode == 0, "conflicts": conflicts}
    finally:
        _run(["git", "-C", bare_path, "worktree", "remove", "--force", wt])
        _run(["git", "-C", bare_path, "update-ref", "-d", tmpref])
        shutil.rmtree(wt, ignore_errors=True)


def land_branch(mr: dict) -> dict:
    """Fetch the MR's source branch from the bare clone into the user's real repo,
    so they can review/merge with their own tools. Non-destructive — adds a branch
    ref, never touches the working tree."""
    src = mr["source_branch"]
    r = _run(["git", "-C", mr["source_path"], "fetch", mr["bare_path"], f"{src}:{src}"])
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()[:160]}
    return {"ok": True, "branch": src, "repo": mr["source_path"]}


def merge_branch(mr: dict, target: str) -> dict:
    """Merge the MR's source into *target* in the user's real repo, producing a
    merge commit. Guarded and reversible: refuses to merge over uncommitted work
    on the current branch; when the target isn't checked out, merges via a
    temporary worktree so the user's working tree is never disturbed; aborts on
    conflict. Never forces, never deletes. Returns {ok, sha, target} or error."""
    real, src, bare = mr["source_path"], mr["source_branch"], mr["bare_path"]
    msg = f"Merge {src} into {target} (Sisyphus MR {mr['id']})"

    f = _run(["git", "-C", real, "fetch", bare, f"{src}:{src}"])
    if f.returncode != 0:
        return {"ok": False, "error": f"fetch failed: {f.stderr.strip()[:120]}"}

    cur = _run(["git", "-C", real, "symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip()

    if target == cur:
        if _run(["git", "-C", real, "status", "--porcelain"]).stdout.strip():
            return {"ok": False, "error": f"'{target}' is checked out with uncommitted changes — commit or stash first"}
        m = _run(["git", "-C", real, "merge", "--no-ff", "-m", msg, src])
        if m.returncode != 0:
            _run(["git", "-C", real, "merge", "--abort"])
            return {"ok": False, "error": "merge conflict — resolve manually"}
        sha = _run(["git", "-C", real, "rev-parse", "HEAD"]).stdout.strip()
        return {"ok": True, "sha": sha, "target": target}

    # target not checked out — merge in a throwaway worktree on that branch, so
    # the user's working tree is untouched. Updates the real branch ref.
    wt = tempfile.mkdtemp(prefix="sisyphus-merge-")
    try:
        a = _run(["git", "-C", real, "worktree", "add", wt, target])
        if a.returncode != 0:
            return {"ok": False, "error": f"cannot check out '{target}': {a.stderr.strip()[:120]}"}
        m = _run(["git", "-C", wt, "merge", "--no-ff", "-m", msg, src])
        if m.returncode != 0:
            _run(["git", "-C", wt, "merge", "--abort"])
            return {"ok": False, "error": "merge conflict — resolve manually"}
        sha = _run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
        return {"ok": True, "sha": sha, "target": target}
    finally:
        _run(["git", "-C", real, "worktree", "remove", "--force", wt])
        shutil.rmtree(wt, ignore_errors=True)


def merge_resolving(mr: dict, target: str, resolve_fn) -> dict:
    """Merge the MR's source branch into *target* in the user's real repo, calling
    ``resolve_fn(path, conflicted_text) -> resolved_text | None`` for each file that
    conflicts. A clean merge just commits; on any unresolved file it aborts and
    leaves *target* untouched. Returns {ok, sha, target, resolved} or {ok: False,
    error}. *resolved* is the list of files the resolver had to fix.

    Where the merge happens mirrors merge_branch: in the user's checkout when
    *target* is the branch they have out (guarded on a clean tree), otherwise in
    a throwaway worktree so their working tree is never disturbed. This used to
    always take the worktree path, which git refuses for a branch that is
    already checked out — so resolving into an MR's usual target, `main`, failed
    every time with "fatal: 'main' is already used by worktree at ...". Only
    plan branches, which are never checked out, ever reached the resolver.
    """
    real, src, bare = mr["source_path"], mr["source_branch"], mr["bare_path"]
    f = _run(["git", "-C", real, "fetch", bare, f"{src}:{src}"])
    if f.returncode != 0:
        return {"ok": False, "error": f"fetch failed: {f.stderr.strip()[:120]}"}

    cur = _run(["git", "-C", real, "symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip()
    in_place = (target == cur)
    if in_place:
        if _run(["git", "-C", real, "status", "--porcelain"]).stdout.strip():
            return {"ok": False, "error": f"'{target}' is checked out with uncommitted changes — commit or stash first"}
        wt = real
    else:
        wt = tempfile.mkdtemp(prefix="sisyphus-resolve-")
    resolved_files: List[str] = []
    try:
        if not in_place:
            a = _run(["git", "-C", real, "worktree", "add", wt, target])
            if a.returncode != 0:
                return {"ok": False, "error": f"cannot check out '{target}': {a.stderr.strip()[:120]}"}
        m = _run(["git", "-C", wt, "merge", "--no-commit", "--no-ff", src])
        if m.returncode != 0:
            conflicted = [c for c in _run(["git", "-C", wt, "diff", "--name-only",
                          "--diff-filter=U"]).stdout.splitlines() if c.strip()]
            if not conflicted:
                _run(["git", "-C", wt, "merge", "--abort"])
                return {"ok": False, "error": "merge failed with no resolvable conflicts"}
            for path in conflicted:
                full = os.path.join(wt, path)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    fixed = resolve_fn(path, text)
                    if fixed is None:
                        _run(["git", "-C", wt, "merge", "--abort"])
                        return {"ok": False, "error": f"unresolved conflict in {path}"}
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(fixed)
                    _run(["git", "-C", wt, "add", "--", path])
                    resolved_files.append(path)
                except Exception as exc:  # noqa: BLE001
                    _run(["git", "-C", wt, "merge", "--abort"])
                    return {"ok": False, "error": f"resolver errored on {path}: {exc}"}
            c = _run(["git", "-C", wt, "-c", "user.email=sisyphus@local",
                      "-c", "user.name=Sisyphus", "commit", "-m",
                      f"Merge {src} into {target} (auto-resolved {len(resolved_files)} conflict(s))"])
        else:
            c = _run(["git", "-C", wt, "-c", "user.email=sisyphus@local",
                      "-c", "user.name=Sisyphus", "commit", "--no-edit"])
        if c.returncode != 0:
            return {"ok": False, "error": f"commit failed: {c.stderr.strip()[:120]}"}
        sha = _run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
        return {"ok": True, "sha": sha, "target": target, "resolved": resolved_files}
    finally:
        # Only tear down a worktree we created — never the user's own checkout.
        if not in_place:
            _run(["git", "-C", real, "worktree", "remove", "--force", wt])
            shutil.rmtree(wt, ignore_errors=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_mr_table() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_requests (
            id            TEXT PRIMARY KEY,
            task_id       TEXT NOT NULL,
            repo          TEXT NOT NULL DEFAULT '',
            source_branch TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            description   TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'open',
            bare_path     TEXT NOT NULL DEFAULT '',
            base_branch   TEXT NOT NULL DEFAULT '',
            source_path   TEXT NOT NULL DEFAULT '',
            merge_sha     TEXT,
            merged_at     TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_task ON merge_requests(task_id)")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(merge_requests)").fetchall()}
    if "review_status" not in cols:
        conn.execute("ALTER TABLE merge_requests ADD COLUMN review_status TEXT")
    # The daemon's own verdict on this branch (core.verify), kept apart from
    # review_status: one is what a machine measured, the other what an agent
    # decided, and conflating them is exactly the failure this guards against.
    if "check_status" not in cols:
        conn.execute("ALTER TABLE merge_requests ADD COLUMN check_status TEXT")
        conn.execute("ALTER TABLE merge_requests ADD COLUMN check_output TEXT")
        conn.execute("ALTER TABLE merge_requests ADD COLUMN check_command TEXT")
        conn.execute("ALTER TABLE merge_requests ADD COLUMN check_ran_at TEXT")
    # Where this change lives on a forge, and the last state we observed there.
    # Polling is the only way this daemon hears about a PR it opened — it listens
    # on 127.0.0.1, so no forge can ever call in (core.forge_poll). The poller
    # needs two durable facts per MR: which PR to ask about, and what it looked
    # like last time, because "what changed" is the only thing worth posting to a
    # thread. Kept on the MR row rather than in memory so a daemon restart does
    # not re-announce every open PR's current state as if it were news.
    if "forge_url" not in cols:
        conn.execute("ALTER TABLE merge_requests ADD COLUMN forge_url TEXT")
        conn.execute("ALTER TABLE merge_requests ADD COLUMN forge_state TEXT")
        conn.execute("ALTER TABLE merge_requests ADD COLUMN forge_checked_at TEXT")

    # Which files a model rewrote to make the merge possible, newline-separated.
    # Persisted rather than only flashed after the action: the merge commit looks
    # exactly like a hand-made one, so days later nothing in the UI would say
    # those hunks were written by a model instead of by git.
    if "merge_resolved" not in cols:
        conn.execute("ALTER TABLE merge_requests ADD COLUMN merge_resolved TEXT")

    conn.commit()
    conn.close()


def diff_range(mr: dict) -> dict:
    """The two commits any comparison of this MR must use: {base, head}.

    ``base`` is the merge base of target and source — where the branch left the
    target — not the source branch's previous commit. Both the engineer and a
    reviewer once checked file modes against ``HEAD~1``, concluded nothing had
    changed, and each missed three scripts that had gone 755 → 644 earlier on the
    same branch (docs/known-gaps.md). Telling agents the rule is a prompt;
    handing them the SHA removes the derivation they were getting wrong.

    Returns empty strings for whichever it cannot resolve, so a caller can say
    "unknown" rather than print a plausible wrong value.
    """
    bare = mr.get("bare_path", "")
    target, source = mr.get("target_branch", ""), mr.get("source_branch", "")
    if not (bare and target and source):
        return {"base": "", "head": ""}
    base = _run(["git", "-C", bare, "merge-base", target, source])
    head = _run(["git", "-C", bare, "rev-parse", source])
    return {
        "base": base.stdout.strip() if base.returncode == 0 else "",
        "head": head.stdout.strip() if head.returncode == 0 else "",
    }


def set_check_result(mr_id: str, status: str, output: str = "", command: str = "") -> None:
    """Record what the daemon's own check found: 'passed', 'failed' or
    'not_run'. Never derived from anything an agent said."""
    conn = get_conn()
    now = _now()
    conn.execute(
        "UPDATE merge_requests SET check_status = ?, check_output = ?, "
        "check_command = ?, check_ran_at = ?, updated_at = ? WHERE id = ?",
        (status, output, command, now, now, mr_id),
    )
    conn.commit()
    conn.close()


# ── Forge-side state (what core.forge_poll observed on GitHub/GitLab) ────


def set_forge_url(mr_id: str, forge_url: str) -> None:
    """Record which PR/MR on a forge this row corresponds to.

    Separate from create_mr because the URL is not always known when the row is
    written: a local MR has none, and an MR recovered from the task thread's
    ``mr`` message learns its URL on the first poll. Writing it back means the
    thread scan happens once per MR, not once per tick.
    """
    conn = get_conn()
    conn.execute("UPDATE merge_requests SET forge_url = ?, updated_at = ? WHERE id = ?",
                 (forge_url, _now(), mr_id))
    conn.commit()
    conn.close()


def get_forge_state(mr_id: str) -> Optional[dict]:
    """The last state we successfully read off the forge, or None if we never
    have. None and ``{}`` are different answers and callers depend on it: None
    means "never observed" (so nothing on the forge is news yet), while a stored
    dict means every field in it is a baseline to diff against."""
    row = get_mr(mr_id)
    raw = (row or {}).get("forge_state")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def save_forge_state(mr_id: str, state: dict) -> None:
    """Persist a state we actually read from the forge.

    Must only be called after a successful read. A failed read that stored
    anything here would turn "we could not reach the forge" into "the PR is
    unchanged", and the merge/review the poller exists to notice would be lost
    for good rather than reported one tick later.
    """
    now = _now()
    conn = get_conn()
    conn.execute(
        "UPDATE merge_requests SET forge_state = ?, forge_checked_at = ?, updated_at = ? "
        "WHERE id = ?",
        (json.dumps(state, sort_keys=True), now, now, mr_id),
    )
    conn.commit()
    conn.close()


def set_review_status(mr_id: str, review_status: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE merge_requests SET review_status = ?, updated_at = ? WHERE id = ?",
        (review_status, _now(), mr_id),
    )
    conn.commit()
    conn.close()


def _dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def create_mr(task_id: str, repo: str, source_branch: str, target_branch: str,
              title: str = "", description: str = "", bare_path: str = "",
              base_branch: str = "", source_path: str = "",
              forge_url: str = "") -> dict:
    mr_id = f"mr-{uuid4().hex[:8]}"
    now = _now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO merge_requests (id, task_id, repo, source_branch, target_branch,
           title, description, status, bare_path, base_branch, source_path,
           forge_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
        (mr_id, task_id, repo, source_branch, target_branch, title, description,
         bare_path, base_branch, source_path, forge_url, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    conn.close()
    return _dict(row)


def get_mr(mr_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    conn.close()
    return _dict(row)


def open_mr_exists(task_id: str) -> bool:
    """True if the task has an unresolved local MR — the per-task bare clone must
    survive cleanup so the MR can still be landed/merged."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM merge_requests WHERE task_id = ? AND status = 'open'",
        (task_id,)).fetchone()[0]
    conn.close()
    return n > 0


def find_open_mr(task_id: str, source_branch: str, target_branch: str) -> Optional[dict]:
    """Return the open MR matching (task_id, source_branch, target_branch), or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM merge_requests WHERE task_id = ? AND source_branch = ? "
        "AND target_branch = ? AND status = 'open'",
        (task_id, source_branch, target_branch),
    ).fetchone()
    conn.close()
    return _dict(row)


def update_mr(mr_id: str, title: str = "", description: str = "") -> Optional[dict]:
    """Refresh an existing MR's title/description/updated_at. Returns the updated row."""
    conn = get_conn()
    conn.execute(
        "UPDATE merge_requests SET title = ?, description = ?, updated_at = ? WHERE id = ?",
        (title, description, _now(), mr_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    conn.close()
    return _dict(row)


def latest_for_task(task_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM merge_requests WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
        (task_id,)).fetchone()
    conn.close()
    return _dict(row)


def list_mrs(status: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM merge_requests WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM merge_requests ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_target(mr_id: str, target_branch: str) -> Optional[dict]:
    conn = get_conn()
    conn.execute("UPDATE merge_requests SET target_branch = ?, updated_at = ? WHERE id = ?",
                 (target_branch, _now(), mr_id))
    conn.commit()
    conn.close()
    return get_mr(mr_id)


def set_status(mr_id: str, status: str) -> None:
    if status not in MR_STATUSES:
        raise ValueError(f"invalid MR status {status!r}; must be one of {MR_STATUSES}")
    conn = get_conn()
    conn.execute("UPDATE merge_requests SET status = ?, updated_at = ? WHERE id = ?",
                 (status, _now(), mr_id))
    conn.commit()
    conn.close()
    if status in ("merged", "closed"):
        reclaim_if_settled(mr_id)


def mark_merged(mr_id: str, merge_sha: str, resolved: Optional[List[str]] = None) -> None:
    """Record the merge. *resolved* is the file list ``merge_resolving`` returns —
    the files a model had to rewrite. An ordinary merge passes nothing and stores
    an empty string, so "was this auto-resolved?" is answered by the row itself
    and never inferred from the absence of a flash message."""
    now = _now()
    conn = get_conn()
    conn.execute(
        "UPDATE merge_requests SET status = 'merged', merge_sha = ?, merged_at = ?, "
        "merge_resolved = ?, updated_at = ? WHERE id = ?",
        (merge_sha, now, "\n".join(resolved or []), now, mr_id))
    conn.commit()
    conn.close()
    reclaim_if_settled(mr_id)


def reclaim_if_settled(mr_id: str) -> bool:
    """Reclaim a task's disk once its last open merge request closes.

    core.worker.cleanup_task deliberately keeps the per-task bare clone while an
    MR is still open — the branch a merge lands from lives there. But cleanup
    runs when the *task* reaches a terminal status, which is always before the
    human merges, so nothing ever reclaimed it afterwards. Four finished tasks
    had left 26MB of bare clones and two worktrees behind by the time anyone
    looked; on a real repository that is per task, indefinitely.

    Best-effort and safe to call twice: cleanup_task is idempotent, and this
    does nothing while any MR for the task is still open.

    Deliberately skipped while a worker is still running for the task: a human
    can merge an MR while the task's own reviewer is mid-run, and reclaiming
    then rips the worktree out from under a live process (observed — the
    reviewer kept running against a directory that no longer existed and its
    worker_finished, the event the orchestrator sleeps on, arrived only via
    the timeout path). Nothing is lost by skipping: cleanup_task runs again
    when the task itself reaches a terminal status.
    """
    mr = get_mr(mr_id)
    if not mr:
        return False
    task_id = mr.get("task_id", "")
    if not task_id or open_mr_exists(task_id):
        return False
    from . import sessions
    if sessions.list_for_task(task_id):
        return False
    try:
        from .worker import cleanup_task
        cleanup_task(task_id)
        return True
    except Exception:  # noqa: BLE001 — reclaiming disk must never fail a merge
        import logging
        logging.getLogger(__name__).exception("could not reclaim disk for %s", task_id)
        return False
