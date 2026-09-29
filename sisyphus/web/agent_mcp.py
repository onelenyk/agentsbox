"""Agent-facing MCP server (HTTP) — the worker's tool surface.

A spawned worker (opencode) connects here over HTTP with a Bearer token that
scopes it to one (task, role). ``tools/list`` returns exactly that role's tools;
``tools/call`` writes to the task thread. "When to use a tool" is enforced by
preconditions *inside the tool*, not by the prompt. This replaces the old
file-based agent contract (events.jsonl / result.json / dir-scan).

Speaks MCP JSON-RPC 2.0 over HTTP: one POST endpoint, one JSON response per
request (notifications get 202). Mounted in the daemon where the DB is writable.
"""
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core import agents, attachments, mergerequests, reviews, sessions, store

agent_mcp = APIRouter()
logger = logging.getLogger(__name__)

# Phase 3 sets this to enqueue an orchestrator event when a worker finishes.
# None = no-op (Phase 2 has no event bus yet).
on_worker_finished = None


def _end_worker(session: dict) -> None:
    store.post_message(
        session["task_id"], author="system", kind="worker_finished",
        text=f"{session['role']} finished",
    )
    sessions.revoke(session["token"])
    if on_worker_finished is not None:
        on_worker_finished(session["task_id"])


# ── Tool handlers (each writes to the task thread) ──────────────────────


def _get_context(session, **_):
    task = store.get_task(session["task_id"])
    return {"goal": task["goal"] if task else "", "thread": store.render_thread(session["task_id"], for_prompt=True)}


def _comment(session, text="", **_):
    if not text.strip():
        return {"error": "text required"}
    store.post_message(session["task_id"], session["role"], kind="note", text=text)
    return {"ok": True}


def _attach(session, filename="", description="", **_):
    if not filename or "/" in filename or "\\" in filename:
        return {"error": "filename must be a bare name"}
    from core import attachments

    if not attachments.save_from_worktree(session["task_id"], session.get("worktree", ""), filename):
        return {"error": f"no file named {filename!r} in your working dir to attach"}
    store.post_message(
        session["task_id"], session["role"], kind="note",
        text=description or f"attached {filename}", attachment=filename,
    )
    return {"ok": True}


# A view is a whole page, not a note — big enough for a 50-row table with its
# own styles and script, small enough that a runaway loop can't fill the disk.
VIEW_MAX_BYTES = 1_000_000


def _show_html(session, title="", html="", **_):
    """Put a self-contained HTML page into the task — shown in the result
    area in a sandboxed frame (see web.routes.task_view)."""
    if not html.strip():
        return {"error": "html required: a complete, self-contained page"}
    data = html.encode("utf-8")
    if len(data) > VIEW_MAX_BYTES:
        return {"error": f"html is {len(data)} bytes; the limit is {VIEW_MAX_BYTES}"}
    from core import attachments

    name = attachments.save_bytes(session["task_id"], "view.html", data)
    store.post_message(session["task_id"], session["role"], kind="view",
                       text=(title or "").strip() or "view", attachment=name)
    return {"ok": True, "file": name}


def _propose_subtasks(session, items=None, **_):
    """An explorer's structured fan-out proposal: one {repo, goal} per repo. Posts
    a 'plan' message the orchestrator turns into child tasks (spawn_task)."""
    items = items or []
    if not items:
        return {"error": "items required: a list of {repo, goal} objects"}
    lines = []
    for it in items:
        goal = (it.get("goal") or "").strip()
        if not goal:
            return {"error": "each item needs a goal"}
        repo = (it.get("repo") or "").strip()
        lines.append(f"- [{repo or 'no repo'}] {goal}")
    store.post_message(session["task_id"], session["role"], kind="plan",
                       text="Proposed subtasks:\n" + "\n".join(lines))
    return {"ok": True, "count": len(items)}


def _finish(session, summary="", **_):
    if not summary.strip():
        return {"error": "summary required"}
    # A review task carries a verdict for a specific MR, and only approve /
    # request_changes write it back. Finishing with prose strands the MR at
    # "pending" forever, so refuse here rather than trust the prompt — the
    # agent gets told which tool to use instead.
    task = store.get_task(session["task_id"])
    if task and reviews.parse_review_marker(task.get("goal", "")):
        return {"error": "this is a review task — finish it with "
                         "approve(summary=...) or request_changes(reason=...) "
                         "so the verdict reaches the merge request"}
    store.post_message(session["task_id"], session["role"], kind="result", text=summary)
    _end_worker(session)
    return {"ok": True}


def _commit(session, message="", allow_mode_change=False, **_):
    if not message.strip():
        return {"error": "commit message required"}
    from core import git_ops, repos

    if not git_ops.is_git_repo(session.get("worktree", "")):
        return {"error": "this task has no repo — commit needs one; report your work with finish() instead"}

    task = store.get_task(session["task_id"])
    repo_name = task.get("repo") if task else None
    if repo_name and repos.is_read_only(repo_name):
        return {"error": f"repo {repo_name} is read-only: report findings with finish() instead"}

    # The exec-bit guard: a staged 100755 -> 100644 change with no content
    # change is a sandbox checkout artifact (build.sh / goal_report.py lost it
    # four times), not work. Restore the bit before committing and say so in
    # the result, unless the goal explicitly asks for a mode change or the
    # caller opts out.
    goal = task.get("goal") if task else ""
    try:
        restored = git_ops.restore_dropped_exec_bits(
            session["worktree"], goal=goal, allow_mode_change=allow_mode_change)
        sha = git_ops.commit_all(session["worktree"], message)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"commit failed: {exc}"}
    if sha is None:
        return {"error": "nothing to commit — no changes in the working tree"}
    note = ""
    if restored:
        note = f" restored exec bit on {', '.join(restored)}"
        store.post_message(session["task_id"], session["role"], kind="note",
                           text=note)
    store.post_message(session["task_id"], session["role"], kind="commit",
                       text=f"{message} ({sha[:8]}){note}")
    result = {"ok": True, "sha": sha}
    if restored:
        result["restored_exec_bit"] = restored
    return result


def _open_mr(session, title="", description="", **_):
    if not any(m["kind"] == "commit" for m in store.get_thread(session["task_id"])):
        return {"error": "cannot open a merge request before a commit"}
    from core.providers import publish_change
    from core import repos

    task = store.get_task(session["task_id"])
    repo_name = task.get("repo") if task else None
    if repo_name and repos.is_read_only(repo_name):
        return {"error": f"repo {repo_name} is read-only: report findings with finish() instead"}

    result = publish_change(session, title, description)
    if "error" in result:
        return result

    # Compose the thread record. For a local repo the change IS the diff — show
    # the branch + files-changed summary and point at the saved patch (viewable
    # once attachment serving lands, roadmap #3). For a forge it'll be the URL.
    if result.get("platform") == "local":
        text = (f"{title}\n\nBranch: {result['branch']}\n\n{result['stat']}").strip()
        store.post_message(session["task_id"], session["role"], kind="mr",
                           text=text, attachment=result["patch_file"])
    else:
        text = f"{title}: {result.get('url', '')}".strip(": ")
        store.post_message(session["task_id"], session["role"], kind="mr", text=text)

    _spawn_review_hook(session["task_id"])

    return {"ok": True, "branch": result.get("branch")}


def _review_hook(task_id: str) -> None:
    """Check the MR and stand up its review task. MUST NOT run on the MCP
    request path: ensure_review_task runs the repo's own check command — 150s+
    of pytest on this repo — and an MCP client waits ~60s before declaring the
    server dead. When that happened the runner dropped every sisyphus tool,
    the engineer retried open_mr 160 times against an empty roster, burned to
    the 1800s total timeout, and the orchestrator respawned a second engineer
    to redo finished work (task-60088d8f, 2026-08-23)."""
    try:
        mr = mergerequests.latest_for_task(task_id)
        if mr:
            task = reviews.ensure_review_task(mr)
            if task:
                mergerequests.set_review_status(mr["id"], "pending")
                # A created task is inert until an event puts it on the dispatch
                # queue — without this the review task sits `active` forever.
                from core.orchestrator import emit
                emit(task["id"], "created")
            # The check only ever ran inside ensure_review_task, which dedups at
            # the top: a second open_mr on a branch whose head moved never
            # re-checked the new commit, and the merge sat blocked on
            # check-stale (SIS-115, mr-acbf1f50). Re-run whenever the recorded
            # verdict no longer covers the branch. Re-read the row first —
            # ensure_review_task may just have recorded the first check, and the
            # stale copy fetched above would mislead the comparison.
            fresh = mergerequests.get_mr(mr["id"])
            if fresh and mergerequests.needs_recheck(fresh):
                from core import verify
                res = verify.run_and_record(fresh)
                if task is None:
                    # A review task already exists — its goal carries the old
                    # check line, so post the new verdict on its thread for the
                    # reviewer to read (same format the goal embeds).
                    existing = reviews.find_review_task(fresh)
                    if existing:
                        store.post_message(existing["id"], "system", kind="note",
                                           text=verify.summarise(res, head=res.get("head", "")))
    except Exception:
        logger.warning("review hook failed for open_mr on task %s", task_id, exc_info=True)


_last_review_hook: "Optional[threading.Thread]" = None


def _spawn_review_hook(task_id: str) -> "threading.Thread":
    """Run _review_hook on a daemon thread; open_mr's reply returns at once.
    The thread handle is kept in _last_review_hook so a test can join it."""
    global _last_review_hook
    t = threading.Thread(target=_review_hook, args=(task_id,),
                         daemon=True, name=f"review-hook-{task_id}")
    _last_review_hook = t
    t.start()
    return t


def _starts_with_verdict(text: str, verdict: str) -> bool:
    return (text or "").strip().upper().startswith(verdict)


def _verdict_result_text(verdict: str, body: str) -> str:
    """The reviewer's own result line, saying the verdict exactly once.

    Reviewers naturally open with the verdict word, so prefixing blindly gave
    "APPROVED: APPROVED — 1 MR reviewed". Text that already leads with the
    verdict is kept verbatim; the task-detail template keys its badge off
    startswith(), so the verdict must stay at the front either way.
    """
    b = (body or "").strip()
    return b if _starts_with_verdict(b, verdict) else f"{verdict}: {b}".strip(": ").strip()


def _verdict_body(verdict: str, body: str) -> str:
    """Just the reasoning, for embedding after a verdict word somewhere else.

    The first fix for the stutter touched only the result line and missed the
    cross-post to the origin task, so "APPROVED — APPROVED — ..." simply moved
    there instead. Both call sites go through these helpers now.
    """
    b = (body or "").strip()
    if _starts_with_verdict(b, verdict):
        b = b[len(verdict):].lstrip(" :—-")
    return b


def _wake_origin(origin_task_id: str) -> None:
    """A verdict on a branch is news for the task that made it. Nothing used to
    carry it there: the note landed on the origin thread and no event followed,
    so an origin task with nothing else running would have waited forever for a
    pass. It only ever moved because a SECOND reviewer of its own happened to
    finish and wake it — which is exactly the duplicate this removes."""
    try:
        from core.orchestrator import emit
        emit(origin_task_id, "external")
    except Exception:  # noqa: BLE001 — the verdict is recorded either way
        logger.warning("could not wake origin task %s after a verdict", origin_task_id, exc_info=True)


def _record_verdict_on_own_mr(task_id: str, verdict: str) -> None:
    """A task's own reviewer approves on the thread; the review-gated merge
    reads mr.review_status. Only the Review-of-MR path (the marker branch of
    _approve/_request_changes) ever called set_review_status, so an approved
    change still showed `pending` and the gate refused the human's merge until
    the verdict was recorded by hand. Record it on the task's open MR too."""
    mr = mergerequests.latest_for_task(task_id)
    if mr and mr.get("status") == "open":
        mergerequests.set_review_status(mr["id"], verdict)


def _approve(session, summary="", **_):
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=_verdict_result_text("APPROVED", summary))
    try:
        task = store.get_task(session["task_id"])
        if task:
            marker = reviews.parse_review_marker(task["goal"])
            if marker:
                mergerequests.set_review_status(marker["mr_id"], "approved")
                if marker["origin_task"]:
                    store.post_message(
                        marker["origin_task"], author="system", kind="note",
                        text=(f"review verdict for {marker['mr_id']}: APPROVED — "
                              f"{_verdict_body('APPROVED', summary)}"),
                    )
                    _wake_origin(marker["origin_task"])
            else:
                _record_verdict_on_own_mr(session["task_id"], "approved")
    except Exception:
        logger.warning("review bookkeeping failed for approve on task %s", session["task_id"], exc_info=True)
    _end_worker(session)
    return {"ok": True, "verdict": "approved"}


# ── Orchestrator decision tools ─────────────────────────────────────────
# The orchestrator agent calls exactly one of these per pass. They do NOT
# execute the action — they record the chosen Decision on the session, which
# core.orchestrator._decide reads back and hands to _execute (so all the
# spawn/worktree/disk logic stays in one place and the guards still apply).


def _record(session, decision: dict):
    # Exactly ONE decision per pass, enforced. Without this the decider can
    # call decide_* repeatedly in one session — each call silently overwrote
    # the last, {ok:true} read as "it happened", and the model spiraled into
    # "I spawned 3 workers and nothing appeared" → ask_human (live incident,
    # task-1e23e9e8). First decision wins; later calls get a hard error.
    if session.get("decision") is not None:
        return {"error": "decision already recorded for this pass — it executes "
                         "AFTER your session ends. Do not call another decide_* "
                         "tool; end your turn now."}
    session["decision"] = decision
    return {"ok": True, "note": "decision recorded; it executes after this session ends — end your turn"}


def _decide_spawn_worker(session, agent="", instruction="", why="", **_):
    if not agent.strip():
        return {"error": "agent required (the worker agent to run)"}
    # Validate against the real roster so a hallucinated name (e.g. a weak model
    # inventing 'code') is rejected here — the orchestrator agent sees the error
    # and re-picks a valid agent in the same session, instead of it becoming a
    # failed spawn later.
    from core import agents
    a = agents.get_agent(agent)
    if not a or a["kind"] != "worker":
        names = ", ".join(x["name"] for x in agents.list_agents(kind="worker"))
        return {"error": f"unknown agent '{agent}'. Call decide_spawn_worker again with exactly one of: {names}"}
    return _record(session, {"action": "spawn_worker", "role": agent,
                             "instruction": instruction, "why": why})


def _decide_spawn_task(session, subtasks=None, why="", **_):
    if not subtasks:
        return {"error": "subtasks required: a list of {repo, goal}"}
    return _record(session, {"action": "spawn_task", "subtasks": subtasks, "why": why})


def _decide_ask_human(session, question="", why="", **_):
    if not question.strip():
        return {"error": "question required"}
    return _record(session, {"action": "ask_human", "question": question, "why": why})


def _decide_answer(session, text="", why="", **_):
    if not text.strip():
        return {"error": "text required — this IS the answer the human reads"}
    return _record(session, {"action": "answer", "text": text, "why": why or "answered the question"})


def _decide_complete(session, why="", **_):
    return _record(session, {"action": "done", "why": why})


def _decide_fail(session, reason="", why="", **_):
    return _record(session, {"action": "fail", "reason": reason, "why": why or reason})


def _decide_note(session, text="", why="", **_):
    return _record(session, {"action": "note", "why": why or text})


def _request_changes(session, reason="", **_):
    if not reason.strip():
        return {"error": "reason required for request_changes"}
    store.post_message(session["task_id"], session["role"], kind="result",
                       text=_verdict_result_text("CHANGES REQUESTED", reason))
    try:
        task = store.get_task(session["task_id"])
        if task:
            marker = reviews.parse_review_marker(task["goal"])
            if marker:
                mergerequests.set_review_status(marker["mr_id"], "changes_requested")
                if marker["origin_task"]:
                    store.post_message(
                        marker["origin_task"], author="system", kind="note",
                        text=(f"review verdict for {marker['mr_id']}: CHANGES REQUESTED — "
                              f"{_verdict_body('CHANGES REQUESTED', reason)}"),
                    )
                    _wake_origin(marker["origin_task"])
            else:
                _record_verdict_on_own_mr(session["task_id"], "changes_requested")
    except Exception:
        logger.warning("review bookkeeping failed for request_changes on task %s", session["task_id"], exc_info=True)
    _end_worker(session)
    return {"ok": True, "verdict": "changes_requested"}


def _list_mrs(session, repo="", **_):
    mrs = mergerequests.list_mrs(status="open")
    return [{"id": m["id"], "task_id": m["task_id"], "title": m["title"],
             "branch": m["source_branch"], "target_branch": m["target_branch"],
             "status": m["status"], "created_at": m["created_at"]} for m in mrs
            if not repo or m["repo"] == repo]


def _recorded_check(mr: dict, head: str) -> dict:
    """The verdict the DAEMON itself reached on this branch, handed over with
    the diff.

    A review-of-MR task is a different task from the one that produced the MR,
    so core.verify's summary — posted to the ORIGIN task's thread — never
    reached the reviewer reading it. The persona was told the recorded check is
    authoritative over its own sandboxed run, and then had no way to see it:
    three of the eleven human questions in the ledger are a reviewer asking a
    human to adjudicate pre-existing environmental failures while a green
    machine verdict for the same branch sat in the database unread.

    ``covers_head`` is the honest half. A verdict is authoritative only for the
    commit it ran against; if the branch has moved since, it is history, and
    saying so is the difference between evidence and a number that merely looks
    like one. When the record is silent about that commit — every MR checked
    before the check_head column existed — it is a gap in our own bookkeeping,
    not a sign the branch moved, and the reviewer has to re-run the check rather
    than be told the verdict is stale.
    """
    status = (mr.get("check_status") or "").strip()
    if not status:
        return {"status": "none",
                "meaning": ("No automated check is recorded for this branch. You have "
                            "no machine verdict to lean on — judge the diff itself, and "
                            "say plainly what you could not verify.")}

    ran_head = (mr.get("check_head") or "").strip()
    covers = bool(ran_head) and bool(head) and ran_head == head
    out = (mr.get("check_output") or "").strip().splitlines()
    check = {
        "status": status,                       # passed | failed | not_run
        "command": mr.get("check_command") or "",
        "ran_at": mr.get("check_ran_at") or "",
        "ran_against": ran_head,
        "covers_head": covers,
        "output_tail": "\n".join(out[-12:]),
    }
    if status == "not_run":
        check["meaning"] = ("The daemon tried and had nothing to run — this repo "
                            "configures no check command. Absence of a check is not a "
                            "pass; judge the diff itself.")
    elif not covers:
        # A verdict recorded before the check_head column existed (every MR
        # checked before 2026-08-24) carries no commit at all. Claiming it is
        # stale accuses the branch of moving when the record just never said
        # what it covered — the reviewer must re-run the check, not take that
        # silence as proof of anything.
        if not ran_head:
            check["meaning"] = (
                "The commit this verdict ran against was never recorded, so coverage "
                "cannot be determined. Do NOT treat it as covering the current diff — "
                "re-run the check itself before you lean on it.")
        else:
            check["meaning"] = (
                f"This verdict was recorded against {ran_head[:12]}, and the branch head "
                f"is now {head[:12] or 'unknown'}. It does NOT cover the current diff — "
                f"treat this branch as unverified.")
    elif status == "passed":
        check["meaning"] = (
            "The daemon ran this command itself, unsandboxed, against exactly this "
            "commit, and it passed. It is authoritative over your own run: your "
            "worktree is sandboxed and fails some tests for environmental reasons. "
            "Do NOT ask a human to adjudicate a failure this record contradicts — "
            "say which test failed for you, note that the recorded check passed, and "
            "give your verdict on the diff.")
    else:
        check["meaning"] = (
            "The daemon ran this command itself against exactly this commit and it "
            "FAILED. This is not environmental and not yours to dismiss — the failing "
            "output is below. Request changes.")
    return check


def _get_mr_diff(session, mr_id="", **_):
    mr = mergerequests.get_mr(mr_id)
    if not mr:
        return {"error": f"merge request {mr_id!r} not found"}

    # Hand over the two commits any further comparison must use. Agents that ran
    # their own git reached for HEAD~1 — the previous commit on the branch — and
    # drew confident, wrong conclusions from it; see mergerequests.diff_range.
    rng = mergerequests.diff_range(mr)
    baseline = {
        "base_sha": rng["base"],
        "head_sha": rng["head"],
        "baseline": (
            f"Compare against {rng['base'][:12] or 'the merge base'}, the point this "
            f"branch left {mr.get('target_branch', 'the target')}. If you run git "
            f"yourself, use that SHA — never HEAD~1, which is only the previous "
            f"commit on this branch and hides anything changed earlier on it."
        ),
    }

    baseline["recorded_check"] = _recorded_check(mr, rng.get("head", ""))

    bare_path = mr.get("bare_path", "")
    if bare_path:
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", bare_path, "diff", f"{mr['target_branch']}...{mr['source_branch']}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return {"mr_id": mr_id, "source": "fresh", "diff": r.stdout, **baseline}
        except Exception as e:
            logger.debug("get_mr_diff: fresh diff failed for %s, falling back to snapshot: %s", mr_id, e)

    diff = attachments.read(mr["task_id"], "change.patch")
    if diff is not None:
        return {"mr_id": mr_id, "source": "snapshot", "diff": diff, **baseline}
    return {"error": f"no diff found for MR {mr_id!r}"}


def _jira_issue(session, key="", **_):
    from core import jira

    if not jira.configured():
        return {"error": "Jira is not configured on this daemon"}

    # If no key is given, try to extract it from the task's origin
    if not key.strip():
        task = store.get_task(session["task_id"])
        origin = (task.get("origin") or "") if task else ""
        if origin.startswith("jira:"):
            key = origin[5:]  # Remove "jira:" prefix
        else:
            return {"error": "no key provided and task has no Jira origin"}

    key = key.strip()
    actor = f"agent:{session['role']}@{session['task_id']}"

    try:
        return jira.get_issue(key, actor, task_id=session["task_id"])
    except jira.JiraError as exc:
        return {"error": str(exc)}


def _jira_search(session, jql="", max_results=50, **_):
    from core import jira

    if not jira.configured():
        return {"error": "Jira is not configured on this daemon"}
    if not jql.strip():
        return {"error": "jql required"}
    try:
        n = max(1, min(int(max_results or 50), 100))
    except (TypeError, ValueError):
        n = 50
    actor = f"agent:{session['role']}@{session['task_id']}"
    try:
        issues = jira.search(jql.strip(), actor, max_results=n, task_id=session["task_id"])
    except jira.JiraError as exc:
        return {"error": str(exc)}
    # Fifty full descriptions would eat the agent's context for nothing; the
    # full text of any one issue is a jira_issue call away.
    for it in issues:
        d = it["fields"].get("description") or ""
        if len(d) > 400:
            it["fields"]["description"] = d[:400] + "…"
    return {"count": len(issues), "issues": issues}


def _jira_transition(session, to_status="", key="", **_):
    from core import jira

    if not jira.configured():
        return {"error": "Jira is not configured on this daemon"}

    if not to_status.strip():
        return {"error": "to_status required"}

    # If no key is given, try to extract it from the task's origin
    if not key.strip():
        task = store.get_task(session["task_id"])
        origin = (task.get("origin") or "") if task else ""
        if origin.startswith("jira:"):
            key = origin[5:]  # Remove "jira:" prefix
        else:
            return {"error": "no key provided and task has no Jira origin"}

    key = key.strip()
    to_status = to_status.strip()
    actor = f"agent:{session['role']}@{session['task_id']}"

    try:
        result = jira.transition(key, to_status, actor, task_id=session["task_id"])
        # Post a thread note after success
        store.post_message(
            session["task_id"], author="system", kind="note",
            text=f"moved {result['key']} to {result['status']} in Jira"
        )
        return result
    except jira.JiraError as exc:
        return {"error": str(exc)}


def _jira_comment(session, text="", key="", **_):
    from core import jira

    if not jira.configured():
        return {"error": "Jira is not configured on this daemon"}

    if not text.strip():
        return {"error": "text required"}

    # If no key is given, try to extract it from the task's origin
    if not key.strip():
        task = store.get_task(session["task_id"])
        origin = (task.get("origin") or "") if task else ""
        if origin.startswith("jira:"):
            key = origin[5:]  # Remove "jira:" prefix
        else:
            return {"error": "no key provided and task has no Jira origin"}

    key = key.strip()
    text = text.strip()
    actor = f"agent:{session['role']}@{session['task_id']}"

    try:
        result = jira.comment(key, text, actor, task_id=session["task_id"])
        # Post a thread note after success
        store.post_message(
            session["task_id"], author="system", kind="note",
            text=f"commented on {result['key']} in Jira"
        )
        return result
    except jira.JiraError as exc:
        return {"error": str(exc)}


# name -> (description, inputSchema, handler)
TOOLS = {
    "get_context": ("Read the task goal and the full thread so far.",
                    {"type": "object", "properties": {}}, _get_context),
    "comment": ("Post a short progress note to the task.",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, _comment),
    "attach": ("Attach a file you saved in your working dir to the task.",
               {"type": "object", "properties": {"filename": {"type": "string"}, "description": {"type": "string"}}, "required": ["filename"]}, _attach),
    "show_html": ("Show the human a page you generated — cards, a table, a chart — right in the "
                  "task's result area. Use it whenever the answer is a lot of structured data (a "
                  "list of issues, a table, a comparison). Pass one complete, self-contained HTML "
                  "document (inline <style>/<script>; no external files, no network: fetch/XHR are "
                  "blocked, https images and links opening in a new tab work). Style it with the "
                  "UI's own CSS variables, already defined on the page and following the dark/light "
                  "switch: var(--panel) var(--panel-2) var(--bg) var(--border) for surfaces, "
                  "var(--text) var(--text-2) var(--muted) for text, var(--accent) var(--good) "
                  "var(--warn) var(--bad) var(--info) for meaning, var(--sans) var(--mono), "
                  "var(--r-sm) var(--r-md) corners, var(--sp-1)…var(--sp-13) spacing, "
                  "var(--fs-sm) var(--fs-body) var(--fs-lg) sizes. Hard-coded colours will look "
                  "foreign and break in the other theme. After showing the data, keep your finish "
                  "summary short — totals and what stands out; never repeat the rows in text.",
                  {"type": "object", "properties": {"title": {"type": "string"}, "html": {"type": "string"}},
                   "required": ["html"]}, _show_html),
    "propose_subtasks": ("Propose a multi-repo fan-out: one {repo, goal} per repo.",
                         {"type": "object", "properties": {"items": {"type": "array", "items": {
                             "type": "object",
                             "properties": {"repo": {"type": "string"}, "goal": {"type": "string"}},
                             "required": ["goal"]}}}, "required": ["items"]}, _propose_subtasks),
    "finish": ("Report your final result. Mandatory last action.",
               {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}, _finish),
    "commit": ("Commit your changes in the working tree. A tracked file whose only "
               "change is a dropped executable bit (100755 -> 100644, no content "
               "change) gets the bit restored automatically and the result lists "
               "the paths — unless the task goal asks for a mode change or you "
               "set allow_mode_change=true.",
               {"type": "object", "properties": {"message": {"type": "string"},
                                                 "allow_mode_change": {"type": "boolean"}},
                "required": ["message"]}, _commit),
    "open_mr": ("Open a merge request. Requires a prior commit.",
                 {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title"]}, _open_mr),
    "list_mrs": ("List open merge requests and their statuses.",
                 {"type": "object", "properties": {"repo": {"type": "string"}}}, _list_mrs),
    "get_mr_diff": ("Get the diff/patch of a merge request, plus the daemon's own "
                    "recorded check verdict for that exact commit.",
                    {"type": "object", "properties": {"mr_id": {"type": "string"}}, "required": ["mr_id"]}, _get_mr_diff),
    "approve": ("Approve the reviewed work (a reviewer's way to finish).",
                {"type": "object", "properties": {"summary": {"type": "string"}}}, _approve),
    "request_changes": ("Request changes with a reason (a reviewer's way to finish).",
                        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}, _request_changes),
    "jira_issue": ("Read a Jira issue. Without a key, uses the task's own key from origin 'jira:<KEY>'; "
                   "with a key, any issue. Returns the shaped issue (key, url, fields).",
                   {"type": "object", "properties": {"key": {"type": "string"}}}, _jira_issue),
    "jira_search": ("Search Jira with JQL (e.g. 'project = ABC AND status = \"To Do\" ORDER BY priority DESC'). "
                    "Returns up to max_results (default 50, at most 100) issues: key, url, summary, status, "
                    "priority, type, assignee, reporter, labels, dates; descriptions cut to 400 chars.",
                    {"type": "object", "properties": {"jql": {"type": "string"}, "max_results": {"type": "integer"}},
                     "required": ["jql"]}, _jira_search),
    "jira_transition": ("Move this task's Jira issue to a status (e.g. 'In Progress' when you start, "
                        "'In Review' when the MR is open). Only when the task came from Jira.",
                        {"type": "object", "properties": {"to_status": {"type": "string"}, "key": {"type": "string"}},
                         "required": ["to_status"]}, _jira_transition),
    "jira_comment": ("Comment on this task's Jira issue. Only when the task came from Jira.",
                     {"type": "object", "properties": {"text": {"type": "string"}, "key": {"type": "string"}},
                      "required": ["text"]}, _jira_comment),
    # Orchestrator decision tools (kind=orchestrator agents).
    "decide_spawn_worker": ("Decide: run a worker agent on the task.",
                            {"type": "object", "properties": {"agent": {"type": "string"}, "instruction": {"type": "string"}, "why": {"type": "string"}}, "required": ["agent"]}, _decide_spawn_worker),
    "decide_spawn_task": ("Decide: fan out across repos (one child task per {repo, goal}).",
                          {"type": "object", "properties": {"subtasks": {"type": "array", "items": {"type": "object", "properties": {"repo": {"type": "string"}, "goal": {"type": "string"}}, "required": ["goal"]}}, "why": {"type": "string"}}, "required": ["subtasks"]}, _decide_spawn_task),
    "decide_ask_human": ("Decide: pause and ask the human a question.",
                         {"type": "object", "properties": {"question": {"type": "string"}, "why": {"type": "string"}}, "required": ["question"]}, _decide_ask_human),
    "decide_answer": ("Decide: answer the human in the thread and finish — for a task that asks a question rather than asking for a change.",
                      {"type": "object", "properties": {"text": {"type": "string"}, "why": {"type": "string"}}, "required": ["text"]}, _decide_answer),
    "decide_complete": ("Decide: the task is done (needs reviewer approval or all children done).",
                        {"type": "object", "properties": {"why": {"type": "string"}}}, _decide_complete),
    "decide_fail": ("Decide: the task cannot be completed.",
                    {"type": "object", "properties": {"reason": {"type": "string"}, "why": {"type": "string"}}, "required": ["reason"]}, _decide_fail),
    "decide_note": ("Decide: record a thought and take another turn.",
                    {"type": "object", "properties": {"text": {"type": "string"}, "why": {"type": "string"}}}, _decide_note),
}


def _tools_for_agent(agent_name: str) -> list:
    return [
        {"name": name, "description": TOOLS[name][0], "inputSchema": TOOLS[name][1]}
        for name in agents.agent_tools(agent_name) if name in TOOLS
    ]


def _bearer(request: Request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _handle(req: dict, session):
    """Dispatch one JSON-RPC request. Returns a response dict, or None for a
    notification (no id)."""
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sisyphus-agent", "version": "1.0"},
        }}

    if method.startswith("notifications/"):
        return None

    if session is None:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32001, "message": "invalid or missing session token"}}

    if method == "tools/list":
        tools = _tools_for_agent(session["role"])
        if not tools:
            # An empty roster is never a usable answer: a worker with no tools
            # cannot even call finish, so the run can only end at its timeout.
            # It is also the exact shape of the most expensive incident here
            # (docs/known-gaps, 2026-08-23) — an engineer retrying open_mr 160
            # times against nothing. agent_tools() returns [] for an agent that
            # is not in the table, which a live session outlives: deleting a
            # custom agent does not revoke the sessions running as it. Say what
            # happened instead of answering "you have no tools".
            return {"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32002,
                "message": (f"agent {session['role']!r} has no tools — it was deleted or "
                            f"saved with an empty tool list, so this run cannot proceed"),
            }}
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        allowed = agents.agent_tools(session["role"])
        if name not in allowed:
            raw = {"error": f"tool {name!r} not permitted for agent {session['role']!r}"}
        else:
            try:
                raw = TOOLS[name][2](session, **args)
            except TypeError as exc:
                raw = {"error": f"invalid arguments: {exc}"}
            except Exception as exc:  # noqa: BLE001
                raw = {"error": str(exc)}
        import json
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(raw)}],
            "isError": "error" in raw,
        }}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


@agent_mcp.post("/mcp/agent")
async def agent_endpoint(request: Request):
    token = _bearer(request)
    session = sessions.resolve(token) if token else None
    body = await request.json()
    resp = _handle(body, session)
    if resp is None:
        return Response(status_code=202)
    return JSONResponse(resp)
