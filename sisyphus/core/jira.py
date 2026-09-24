"""Jira Cloud client — the one door every Jira call goes through.

Thin on purpose: REST v3 over urllib, Basic auth with the account email and an
API token from config (`jira.base_url`, `jira.email`, `jira.token`, resolved
from .env by core.config). Every public function records what it did in
core.integrations, with the *actor* the caller passes, so the Integrations
page can say "engineer@task-… moved VAN-12 to In Progress" rather than "a
request happened".

Credentials never reach a log line, an event row or an exception message.
"""
import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from . import integrations

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20
PAGE_SIZE = 50

# Fields copied when an issue becomes a task; kept small so a goal stays readable.
ISSUE_FIELDS = ("summary", "description", "issuetype", "priority", "status", "labels",
                "components", "created", "updated", "reporter", "assignee")


class JiraError(RuntimeError):
    """Jira answered with something other than the data asked for. *status*
    is the HTTP status when there was one."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _settings() -> dict:
    from .config import get_config
    base = (get_config("jira.base_url") or "").strip().rstrip("/")
    email = (get_config("jira.email") or "").strip()
    token = (get_config("jira.token") or "").strip()
    # An unresolved ${VAR} from config.example.yaml is "not configured", not a value.
    base, email, token = ("" if v.startswith("${") else v for v in (base, email, token))
    return {"base_url": base, "email": email, "token": token}


def configured() -> bool:
    s = _settings()
    return bool(s["base_url"] and s["email"] and s["token"])


def _request(method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    """One authenticated call. Returns the decoded JSON object ({} for 204).
    Raises JiraError with Jira's own message on any non-2xx, without the
    credentials anywhere in it."""
    s = _settings()
    if not (s["base_url"] and s["email"] and s["token"]):
        raise JiraError("Jira is not configured: set jira.base_url, jira.email and jira.token "
                        "(JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN in .env)")
    url = s["base_url"] + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{s['email']}:{s['token']}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json",
        "Content-Type": "application/json", "User-Agent": "sisyphus-jira/1"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")[:500]
        try:
            msg = "; ".join((json.loads(text).get("errorMessages") or []) or [text])
        except ValueError:
            msg = text
        raise JiraError(f"Jira answered {exc.code} for {method} {path}: {msg or exc.reason}",
                        status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JiraError(f"Jira request failed for {method} {path}: {exc}") from None


def _recorded(action: str, actor: str, subject: str, task_id: Optional[str]):
    """Context helper: run a call, journal success or failure, re-raise."""
    class _Ctx:
        def __enter__(self):
            self.detail, self.count, self.meta = "", 0, {}
            return self

        def __exit__(self, exc_type, exc, tb):
            ok = exc is None
            integrations.record("jira", action, actor, subject=subject, detail=self.detail,
                                count=self.count, ok=ok, error=None if ok else str(exc),
                                task_id=task_id, meta=self.meta)
            return False
    return _Ctx()


# ── reads ────────────────────────────────────────────────────────────────

def myself(actor: str = "human:ui") -> dict:
    """Who the token is: {accountId, displayName, emailAddress}. The
    connection check the Integrations page runs."""
    with _recorded("myself", actor, "", None) as ev:
        me = _request("GET", "/rest/api/3/myself")
        ev.detail = f"connected as {me.get('displayName') or me.get('emailAddress') or '?'}"
        return {k: me.get(k) for k in ("accountId", "displayName", "emailAddress")}


def search(jql: str, actor: str, max_results: int = 200, fields: Optional[List[str]] = None,
           task_id: Optional[str] = None) -> List[dict]:
    """Every issue matching *jql*, paged, up to *max_results*. Each item is
    {key, url, fields: {…ISSUE_FIELDS…}} with description flattened to text."""
    fields = list(fields or ISSUE_FIELDS)
    out: List[dict] = []
    with _recorded("search", actor, jql, task_id) as ev:
        next_token = None
        while len(out) < max_results:
            params = {"jql": jql, "maxResults": min(PAGE_SIZE, max_results - len(out)),
                      "fields": ",".join(fields)}
            if next_token:
                params["nextPageToken"] = next_token
            page = _request("GET", "/rest/api/3/search/jql", params=params)
            for issue in page.get("issues", []):
                out.append(_shape(issue))
            next_token = page.get("nextPageToken")
            if not next_token or page.get("isLast", False) or not page.get("issues"):
                break
        ev.detail = f"fetched {len(out)} issue{'s' if len(out) != 1 else ''}"
        ev.count = len(out)
        return out


def get_issue(key: str, actor: str, task_id: Optional[str] = None) -> dict:
    with _recorded("get", actor, key, task_id) as ev:
        issue = _request("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}",
                         params={"fields": ",".join(ISSUE_FIELDS)})
        ev.detail = f"read {key}"
        ev.count = 1
        return _shape(issue)


def transitions(key: str, actor: str, task_id: Optional[str] = None) -> List[dict]:
    """The moves *key* can make now: [{id, name, to}]."""
    with _recorded("transitions", actor, key, task_id) as ev:
        data = _request("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions")
        out = [{"id": t.get("id"), "name": t.get("name"), "to": (t.get("to") or {}).get("name")}
               for t in data.get("transitions", [])]
        ev.detail = f"{len(out)} transitions available"
        ev.count = len(out)
        return out


# ── writes ───────────────────────────────────────────────────────────────

def transition(key: str, to_status: str, actor: str, task_id: Optional[str] = None) -> dict:
    """Move *key* to the status named *to_status* (matched case-insensitively
    against the transition's target status or its name)."""
    with _recorded("transition", actor, key, task_id) as ev:
        options = transitions(key, actor, task_id)
        want = to_status.strip().lower()
        match = next((t for t in options if (t["to"] or "").lower() == want
                      or (t["name"] or "").lower() == want), None)
        if not match:
            names = ", ".join(t["to"] or t["name"] or "?" for t in options) or "none"
            raise JiraError(f"{key} cannot move to {to_status!r}; available: {names}")
        _request("POST", f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions",
                 body={"transition": {"id": match["id"]}})
        ev.detail = f"moved {key} to {match['to'] or match['name']}"
        ev.count = 1
        ev.meta = {"to": match["to"], "transition": match["name"]}
        return {"key": key, "status": match["to"] or match["name"]}


def comment(key: str, text: str, actor: str, task_id: Optional[str] = None) -> dict:
    with _recorded("comment", actor, key, task_id) as ev:
        body = {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}
        data = _request("POST", f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment", body=body)
        ev.detail = f"commented on {key} ({len(text)} chars)"
        ev.count = 1
        return {"key": key, "comment_id": data.get("id")}


# ── shaping ──────────────────────────────────────────────────────────────

def adf_to_text(node) -> str:
    """Atlassian Document Format → plain text, paragraphs separated by blank
    lines. Good enough for a goal; formatting is not the point."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content", []))
    if t in ("paragraph", "heading", "blockquote", "codeBlock", "listItem", "bulletList",
             "orderedList", "panel", "table", "tableRow"):
        return inner.rstrip("\n") + "\n\n" if t in ("paragraph", "heading", "codeBlock") else inner
    if t == "mention":
        return "@" + ((node.get("attrs") or {}).get("text") or "").lstrip("@")
    return inner


def _shape(issue: dict) -> dict:
    f = issue.get("fields") or {}
    base = _settings()["base_url"]

    def name(x):
        return (x or {}).get("name") if isinstance(x, dict) else x

    return {
        "key": issue.get("key"),
        "id": issue.get("id"),
        "url": f"{base}/browse/{issue.get('key')}" if base else "",
        "fields": {
            "summary": f.get("summary") or "",
            "description": adf_to_text(f.get("description")).strip(),
            "issuetype": name(f.get("issuetype")) or "",
            "priority": name(f.get("priority")) or "",
            "status": name(f.get("status")) or "",
            "labels": list(f.get("labels") or []),
            "components": [name(c) for c in (f.get("components") or [])],
            "created": f.get("created") or "",
            "updated": f.get("updated") or "",
            "reporter": (f.get("reporter") or {}).get("displayName") or "",
            "assignee": (f.get("assignee") or {}).get("displayName") or "",
        },
    }
