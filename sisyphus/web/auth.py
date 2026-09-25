"""Shared-token guard for the daemon's HTTP surface — roadmap #7.

The daemon is unauthenticated by default (single-user, loopback). Once it's
exposed on a network, a shared bearer token gates it. Config:

    api:
      token: ${SISYPHUS_API_TOKEN}   # empty/unset → auth disabled
      allow_localhost: true          # loopback skips the check (dev / local MCP)

The guard covers EVERYTHING except the exemptions below. It used to match only
``/api/v1``, which left the half of the surface that actually does the damage
wide open: the htmx UI is served from ``/api/*`` (no ``/v1``) and carries 51
mutating POST routes — delete a repo, restore a backup, run an autopilot, merge
a branch — plus the HTML pages that render tokens, run logs and diffs, and the
``/mcp/agent`` bridge. A token that gated the read-mostly JSON API while
``POST /api/repos/{name}/delete`` answered to anyone was not protecting the
thing worth protecting.

Two paths stay open regardless: health (liveness probes) and webhooks (forge
callers authenticate by their own secret, not our token). Static assets are
exempt because they are the stylesheet and nothing else.

A browser cannot attach a bearer token to a page navigation, so with a token
configured, remote UI access is deliberately not a thing: reach the UI over an
SSH tunnel (``ssh -L 7677:localhost:7677 host``), which lands on loopback and
takes the ``allow_localhost`` path. That is the intended shape — the token is
for scripted callers, the tunnel is for humans.
"""
import secrets
from fastapi.responses import JSONResponse

from core.config import get_config

# Open regardless of the token: liveness probes and forge callbacks, which
# authenticate by their own secret (see core/webhooks.py).
_EXEMPT_PREFIXES = ("/api/v1/health", "/api/v1/webhooks/", "/static/")

# Headers that mean a proxy sat in front of us, so request.client.host is the
# proxy's address rather than the caller's.
_FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def configured_token():
    """The active API token, or None when auth is disabled. An unresolved
    ``${VAR}`` placeholder (env var not set) counts as disabled."""
    token = get_config("api.token")
    if not token or str(token).startswith("${"):
        return None
    return str(token)


def _is_local(request) -> bool:
    """Whether the caller really is on this host.

    A forwarding header means we are behind a proxy and ``request.client.host``
    is the proxy — which is very often 127.0.0.1. Trusting it there would hand
    the localhost exemption to the whole internet, so the presence of any
    forwarding header disqualifies the request from being called local.
    """
    try:
        headers = request.headers
    except AttributeError:
        headers = {}
    if any(h in headers for h in _FORWARD_HEADERS):
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _wants_html(request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


async def api_token_guard(request, call_next):
    """ASGI middleware: reject un-tokened requests to the whole surface."""
    path = request.url.path
    if not path.startswith(_EXEMPT_PREFIXES):
        token = configured_token()
        if token:
            allow_local = get_config("api.allow_localhost", True)
            bearer = _bearer(request) or ""
            authed = secrets.compare_digest(bearer, token) or (allow_local and _is_local(request))
            if not authed:
                if _wants_html(request):
                    # A browser gets a sentence it can act on rather than a JSON
                    # blob it will render as text.
                    return JSONResponse(
                        {"detail": "this daemon requires an API token; reach the UI "
                                   "over an SSH tunnel to localhost:7677"},
                        status_code=401)
                return JSONResponse({"detail": "invalid or missing API token"}, status_code=401)
    return await call_next(request)


# Webhooks stay exempt from the cross-site check too: forges post there
# cross-origin and authenticate by their own secret (see core/webhooks.py).
_CSRF_EXEMPT_PREFIXES = ("/api/v1/webhooks/",)

# Reads are never blocked; only a request that can mutate state can be a
# CSRF write.
_CSRF_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# Sec-Fetch-Site values a same-site request is allowed to carry. "none" is the
# browser's label for a top-level navigation typed or followed directly (no
# Referer-driven site); "same-origin" is a fetch from our own page.
_SAME_SITE = ("same-origin", "none")


def _is_cross_site(request) -> bool:
    """Whether the browser says a request did not come from our own page.

    Modern browsers send ``Sec-Fetch-Site`` on every request: when it is present
    and not ``same-origin``/``none`` the request is cross-site. Older ones (and
    cross-origin XHR/fetch without fetch-metadata) send ``Origin`` instead —
    compare its host:port to the request's ``Host``, which is the check
    ``_from_this_page`` used for the credential routes. Requests carrying
    neither header (curl, the MCP server, workers, TestClient) pass — the
    browser is the only client that sends these, so their absence means the
    caller is not a page that could have been CSRF'd. Origin is compared as a
    plain host:port string (no scheme-implied default-port normalization),
    matching the removed ``_from_this_page``.
    """
    site = request.headers.get("sec-fetch-site")
    if site and site not in _SAME_SITE:
        return True
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
        return True
    return False


async def csrf_guard(request, call_next):
    """ASGI middleware: refuse cross-site writes to the whole HTTP surface.

    Any website the user has open could otherwise POST a form at
    localhost:7677 and create a task, merge an MR or delete a repo — loopback
    callers are trusted and a browser attaches no token, so nothing else asked
    where a request came from. Only the credential routes used to
    (``_from_this_page`` in web/routes.py). Reads are never blocked and the
    webhook path is exempt because forges call it cross-origin by design.
    """
    if (request.method in _CSRF_WRITE_METHODS
            and not request.url.path.startswith(_CSRF_EXEMPT_PREFIXES)
            and _is_cross_site(request)):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)
