"""Repository connection check — try the repo before a task depends on it.

Adding a repo asks for a name, a source, a branch and a platform, and verifies
none of them. Pick "GitLab" in the dropdown, save, and the repo *looks*
configured: the list shows it, tasks can be pointed at it, an agent will happily
spend twenty minutes and several model-dollars writing code against it. The
first thing that actually touches the forge is core.providers._publish_gitlab,
at the very end of the task — and that is where a missing token finally
surfaces, as "GitLab token not configured", after the work is already paid for.

That is the failure class core.runner_smoke was built for: something that looks
configured and was never tried. So this module is shaped like it — each question
gets its own named stage, and a failure reports *which stage* failed and what to
do about it, instead of a bare boolean.

Three properties this deliberately keeps:

  * "we could not determine" is its own answer, distinct from "no". A forge that
    times out is reported as unknown, never as a rejected credential — blaming
    the token for a dropped network is how people revoke tokens that work.
  * nothing is mutated. can_push is answered from the forge's own permission
    fields, never by pushing a probe branch: a check that writes to the user's
    repository in order to learn whether it may write is not a check.
  * the tier is derived only from what was proved. What the user typed into the
    platform dropdown is the claim under test, not evidence for it.
"""
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Generous enough for a cold TLS handshake to a self-hosted GitLab on a slow
# link, short enough that the UI is not left spinning. Every network call in
# this module is bounded by it; nothing here is allowed to block forever.
DEFAULT_TIMEOUT = 15

# Ordered; the report names the first stage that did not come back ok.
STAGES = ("reachable", "authenticated", "can_push")

OK, FAIL, UNKNOWN, SKIPPED = "ok", "fail", "unknown", "skipped"

# GitLab's member roles. 30 (Developer) is the floor that can push a branch and
# open a merge request; Reporter and Guest can read the project and nothing else,
# which is exactly the configuration that looks fine until publish time.
_GITLAB_MIN_PUSH = 30
_GITLAB_ROLES = {5: "Minimal", 10: "Guest", 20: "Reporter", 30: "Developer",
                 40: "Maintainer", 50: "Owner"}


def _stage(name: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# ── I/O boundary (injectable so tests never touch a network) ─────────────

def _http_get(url: str, headers: dict, timeout: int) -> dict:
    """One bounded GET. Returns {status, body, error, offline}.

    ``offline`` is the whole reason this returns a dict instead of raising: a
    DNS failure and a 401 are the same exception family to a caller that only
    sees "it didn't work", and conflating them turns a flaky VPN into "your
    token is bad".
    """
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            return {"status": getattr(resp, "status", 200), "body": body,
                    "error": "", "offline": False}
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"")[:400].decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return {"status": exc.code, "body": body, "error": raw.strip(), "offline": False}
    except urllib.error.URLError as exc:
        return {"status": None, "body": None, "error": str(exc.reason), "offline": True}
    except (TimeoutError, OSError) as exc:  # socket timeout, refused connection
        return {"status": None, "body": None, "error": str(exc), "offline": True}


def _ls_remote(url: str, timeout: int) -> dict:
    """``git ls-remote --heads`` on *url*. Returns {ok, timed_out, detail, refs}.

    GIT_TERMINAL_PROMPT=0 and ssh BatchMode are not hygiene, they are the point:
    against a private URL with no usable credential git otherwise sits waiting
    for a username on a terminal that does not exist. The timeout would then
    fire and the report would blame the network for what is really a missing
    credential — the exact confusion this module exists to prevent.
    """
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "SSH_ASKPASS": "true",
        "GIT_SSH_COMMAND": env.get("GIT_SSH_COMMAND")
                           or "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    })
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", url],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "timed_out": True, "detail": "", "refs": ""}
    except FileNotFoundError:
        return {"ok": False, "timed_out": False, "detail": "git is not installed", "refs": ""}
    if r.returncode != 0:
        return {"ok": False, "timed_out": False,
                "detail": (r.stderr or r.stdout or "").strip()[:300], "refs": ""}
    return {"ok": True, "timed_out": False, "detail": "", "refs": r.stdout}


# ── credential ───────────────────────────────────────────────────────────

def _credential(name: str, platform: str) -> dict:
    """The token this repo would publish with: {token, source, platform, env_hint}.

    Delegates to core.repos.repo_credential for a saved repo. That is not
    politeness — core.providers reads the credential through that same function,
    so a probe that resolved the token any other way could certify one the
    publisher never picks up.

    The add form asks before there is a repo row to read, and an edit form can
    name a platform the saved row does not have yet; both fall back to the
    global block for the platform actually under test, which is the same
    fallback repo_credential itself would take.
    """
    from core.config import get_config
    from core.repos import _resolved, repo_credential

    if name:
        cred = repo_credential(name)
        if cred.get("token") and (not platform or cred.get("platform") == platform):
            return cred

    cfg = get_config(platform, {}) if platform in ("github", "gitlab") else {}
    shared = _resolved((cfg or {}).get("token"))
    if shared:
        return {"token": shared, "source": "global", "platform": platform, "env_hint": ""}
    hint = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN"}.get(platform, "")
    return {"token": None, "source": None, "platform": platform, "env_hint": hint}


def _api_base(platform: str) -> str:
    from core.config import get_config
    cfg = get_config(platform, {}) or {}
    default = "https://api.github.com" if platform == "github" else "https://gitlab.com"
    api = cfg.get("base_url") or default
    # config leaves ${VAR} literal when the variable is unset; a URL that is
    # really a placeholder must not be dialled.
    if str(api).startswith("${"):
        api = default
    return str(api).rstrip("/")


# ── stages ───────────────────────────────────────────────────────────────

def _is_remote(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def _check_reachable(source: str, branch: str, timeout: int, ls_remote) -> dict:
    if not source:
        return _stage("reachable", FAIL, "no source configured",
                      "set the repo's source to a local path or a git URL")

    if not _is_remote(source):
        p = Path(source).expanduser()
        if not p.exists():
            return _stage("reachable", FAIL, f"no such path: {p}",
                          "check the path — a moved or unmounted checkout looks "
                          "identical to a typo from here")
        # Mirrors core.repos.source_reach: a working copy has .git, a bare clone
        # has HEAD. A plain directory is not something a worker can clone from.
        if not ((p / ".git").exists() or (p / "HEAD").exists()):
            return _stage("reachable", FAIL, f"{p} exists but is not a git repository",
                          "point the source at a git working copy or a bare clone, "
                          "or run `git init` there")
        source = str(p)

    res = ls_remote(source, timeout)
    if res.get("timed_out"):
        return _stage("reachable", UNKNOWN,
                      f"no answer from {source} within {timeout}s",
                      "inconclusive, not a refusal — retry when the network settles")
    if not res.get("ok"):
        return _stage("reachable", FAIL,
                      res.get("detail") or "git could not read this source",
                      "git's own words above — usually a wrong URL, or no read "
                      "access for the credential git is using (ssh key or helper)")

    detail = f"git can read {source}"
    refs = res.get("refs") or ""
    if branch and refs.strip():
        # The branch field is another thing nothing checks. A base branch that
        # does not exist fails at merge-request time, not at task time, so say
        # it now — but it is a note, not a verdict: the source *is* readable.
        if f"refs/heads/{branch}" not in refs:
            found = [ln.split("refs/heads/", 1)[1] for ln in refs.splitlines()
                     if "refs/heads/" in ln][:5]
            detail += (f" — but branch {branch!r} is not there"
                       + (f" (found {', '.join(found)})" if found else ""))
    return _stage("reachable", OK, detail)


def _check_authenticated(platform: str, cred: dict, timeout: int, http) -> tuple:
    """Returns (stage, identity)."""
    if platform not in ("github", "gitlab"):
        return _stage("authenticated", SKIPPED,
                      f"no forge for platform {platform or 'unset'!r} — nothing to "
                      "authenticate against"), ""

    env = cred.get("env_hint") or ("GITHUB_TOKEN" if platform == "github" else "GITLAB_TOKEN")
    if not cred.get("token"):
        return _stage("authenticated", FAIL, f"no {platform} token for this repo",
                      f"set {env} in the environment the daemon runs under, or put "
                      f"a token: on this repo in config.yaml"), ""

    api = _api_base(platform)
    if platform == "github":
        url, headers, field = (f"{api}/user",
                               {"Authorization": f"Bearer {cred['token']}",
                                "Accept": "application/vnd.github+json"},
                               "login")
    else:
        url, headers, field = (f"{api}/api/v4/user",
                               {"PRIVATE-TOKEN": cred["token"]},
                               "username")

    res = http(url, headers, timeout)
    if res.get("offline"):
        return _stage("authenticated", UNKNOWN,
                      f"could not reach {api}: {res.get('error') or 'no route'}",
                      "the token was never judged — this says nothing about whether "
                      "it is valid"), ""
    status = res.get("status")
    if status in (401, 403):
        return _stage("authenticated", FAIL,
                      f"{platform} rejected the token (HTTP {status})",
                      f"expired, revoked, or missing API scope — issue a new one and "
                      f"set {env}"), ""
    if status != 200 or not isinstance(res.get("body"), dict):
        return _stage("authenticated", FAIL,
                      f"unexpected HTTP {status} from {url}: "
                      f"{(res.get('error') or '')[:160]}",
                      "check base_url for this platform — a self-hosted forge behind "
                      "a proxy answers here too"), ""

    who = str(res["body"].get(field) or "") or "an unnamed account"
    src = cred.get("source") or "global"
    return _stage("authenticated", OK,
                  f"authenticated as {who} (using the {src} token)"), who


def _check_can_push(platform: str, source: str, cred: dict, identity: str,
                    auth_status: str, timeout: int, http) -> dict:
    if platform not in ("github", "gitlab"):
        return _stage("can_push", SKIPPED,
                      "local repos publish as a patch — there is nothing to push to")
    if auth_status == UNKNOWN:
        return _stage("can_push", UNKNOWN, "not attempted — the forge was unreachable",
                      "retry the check; permission was never asked about")
    if auth_status != OK:
        return _stage("can_push", SKIPPED,
                      "not attempted — the credential did not authenticate")

    from core.providers import _repo_slug, _resolve_forge_url
    # Resolved exactly the way core.providers does when it publishes, including
    # the origin-remote lookup for a path-registered repo. A probe that computed
    # the target itself would be certifying a URL that publishing never uses.
    forge_url = _resolve_forge_url(source)
    if not forge_url:
        return _stage("can_push", FAIL,
                      f"{source} has no 'origin' remote, so there is no {platform} "
                      f"project to push to",
                      "add an origin remote, or set this repo's platform to local")
    slug = _repo_slug(forge_url)

    if platform == "github":
        return _github_push(slug, cred, identity, timeout, http)
    return _gitlab_push(slug, cred, identity, timeout, http)


def _github_push(slug: str, cred: dict, identity: str, timeout: int, http) -> dict:
    api = _api_base("github")
    url = f"{api}/repos/{slug}"
    res = http(url, {"Authorization": f"Bearer {cred['token']}",
                     "Accept": "application/vnd.github+json"}, timeout)
    if res.get("offline"):
        return _stage("can_push", UNKNOWN, f"could not reach {api}: {res.get('error')}",
                      "inconclusive — retry when the network settles")
    if res.get("status") == 404:
        return _stage("can_push", FAIL,
                      f"{slug} is invisible to this token (HTTP 404)",
                      "GitHub returns 404 rather than 403 for a private repo the "
                      "token cannot see — grant it access, or fix the repo path")
    if res.get("status") != 200 or not isinstance(res.get("body"), dict):
        return _stage("can_push", FAIL,
                      f"HTTP {res.get('status')} from {url}: {(res.get('error') or '')[:160]}",
                      "could not read the repository's permissions")

    perms = res["body"].get("permissions")
    if not isinstance(perms, dict):
        # Some fine-grained tokens get a repository payload with no permissions
        # block. Silence is not consent: say so rather than guess either way.
        return _stage("can_push", UNKNOWN,
                      f"{slug} came back without a permissions block",
                      "GitHub did not say what this token may do — open a throwaway "
                      "PR by hand if you need certainty")
    if perms.get("push") or perms.get("maintain") or perms.get("admin"):
        return _stage("can_push", OK, f"{identity or 'this token'} has write access to {slug}")
    return _stage("can_push", FAIL,
                  f"{identity or 'this token'} has read-only access to {slug}",
                  "grant the account Write on the repository, or use a token with "
                  "the 'repo' scope — a read token opens no pull requests")


def _gitlab_push(slug: str, cred: dict, identity: str, timeout: int, http) -> dict:
    api = _api_base("gitlab")
    url = f"{api}/api/v4/projects/{urllib.parse.quote(slug, safe='')}"
    res = http(url, {"PRIVATE-TOKEN": cred["token"]}, timeout)
    if res.get("offline"):
        return _stage("can_push", UNKNOWN, f"could not reach {api}: {res.get('error')}",
                      "inconclusive — retry when the network settles")
    if res.get("status") == 404:
        return _stage("can_push", FAIL,
                      f"project {slug} is invisible to this token (HTTP 404)",
                      "GitLab hides projects a token may not read — grant it access, "
                      "or fix the project path")
    if res.get("status") != 200 or not isinstance(res.get("body"), dict):
        return _stage("can_push", FAIL,
                      f"HTTP {res.get('status')} from {url}: {(res.get('error') or '')[:160]}",
                      "could not read the project's permissions")

    perms = res["body"].get("permissions") or {}
    levels = [((perms.get(k) or {}) or {}).get("access_level")
              for k in ("project_access", "group_access")]
    levels = [n for n in levels if isinstance(n, int)]
    if not levels:
        # An instance admin, or a project shared through a path this API does not
        # report, gets nulls in both blocks. Unknown, not denied.
        return _stage("can_push", UNKNOWN,
                      f"{slug} reports no membership level for this token",
                      "GitLab did not say what this token may do — it can happen for "
                      "instance admins; open a throwaway MR by hand if you need certainty")
    level = max(levels)
    role = _GITLAB_ROLES.get(level, f"level {level}")
    if level >= _GITLAB_MIN_PUSH:
        return _stage("can_push", OK,
                      f"{identity or 'this token'} is {role} on {slug} — may push and open MRs")
    return _stage("can_push", FAIL,
                  f"{identity or 'this token'} is only {role} on {slug}",
                  f"Developer ({_GITLAB_MIN_PUSH}) is the lowest role that can push a "
                  f"branch and open a merge request — ask for it, or register this "
                  f"repo as local")


# ── entry point ──────────────────────────────────────────────────────────

def check_connection(name: str = "", source: str = "", platform: str = "",
                     branch: str = "", timeout: int = DEFAULT_TIMEOUT,
                     http=None, ls_remote=None) -> dict:
    """Probe one repo — saved (pass *name*) or still in the add form (pass the fields).

    Returns {repo, source, platform, ok, tier, stage, detail, summary, identity,
    stages, duration_ms}. ``stages`` carries one entry per named stage with its
    own status ("ok" / "fail" / "unknown" / "skipped"), what was observed, and
    what to do about it. ``stage`` is the first stage that was not ok, so a
    caller can report a cause the way core.runner_smoke does.

    ``tier`` is "local", "can_open_prs", or "unknown", and is computed from the
    stages alone — a repo does not get credit for the platform its owner picked
    in a dropdown.
    """
    from core.providers import detect_platform
    from core.repos import get_repo

    started = time.monotonic()
    http = http or _http_get
    ls_remote = ls_remote or _ls_remote

    repo = get_repo(name) if name else None
    if repo:
        source = source or repo.get("source", "")
        platform = platform or (repo.get("platform") or "").strip()
        branch = branch or repo.get("branch", "") or ""
    source = (source or "").strip()
    platform = (platform or "").strip() or detect_platform(source)

    cred = _credential(name, platform)

    reachable = _check_reachable(source, branch, timeout, ls_remote)
    authenticated, identity = _check_authenticated(platform, cred, timeout, http)
    can_push = _check_can_push(platform, source, cred, identity,
                               authenticated["status"], timeout, http)
    stages = [reachable, authenticated, can_push]

    # The tier is read off the evidence, never off `platform`. A repo the user
    # labelled "gitlab" that proved nothing is unknown, not gitlab.
    if platform == "local":
        tier = "local" if reachable["status"] == OK else "unknown"
    elif (reachable["status"] == OK and authenticated["status"] == OK
          and can_push["status"] == OK):
        tier = "can_open_prs"
    else:
        tier = "unknown"

    failed = next((s for s in stages if s["status"] in (FAIL, UNKNOWN)), None)
    ok = tier != "unknown"

    if ok and tier == "local":
        summary = "local repo — reachable; changes land as a patch, not a merge request"
    elif ok:
        summary = f"can open merge requests on {platform} as {identity or 'this token'}"
    elif failed is not None:
        verb = "could not determine" if failed["status"] == UNKNOWN else "failed at"
        summary = f"{verb} {failed['name']}: {failed['detail']}"
    else:
        # Every stage skipped: a remote on a host with no publisher.
        summary = (f"reachable, but Sisyphus has no publisher for {platform or 'this host'} "
                   f"— tasks can commit, nothing can open a merge request")

    return {
        "repo": name, "source": source, "platform": platform,
        "ok": ok, "tier": tier,
        "stage": failed["name"] if failed is not None else "",
        "detail": failed["detail"] if failed is not None else summary,
        "summary": summary,
        "identity": identity,
        "stages": stages,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
