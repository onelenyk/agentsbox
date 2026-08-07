"""Shared-token guard for the JSON API (/api/v1) — roadmap #7.

The daemon's API is unauthenticated by default (single-user, loopback). Once
it's exposed on a network, a shared bearer token gates it. Config:

    api:
      token: ${SISYPHUS_API_TOKEN}   # empty/unset → auth disabled
      allow_localhost: true          # loopback skips the check (dev / local MCP)

Two paths stay open regardless: health (liveness probes) and webhooks (forge
callers authenticate by their own secret, not our token)."""
import secrets
from fastapi.responses import JSONResponse

from core.config import get_config

_EXEMPT_PREFIXES = ("/api/v1/health", "/api/v1/webhooks/")


def configured_token():
    """The active API token, or None when auth is disabled. An unresolved
    ``${VAR}`` placeholder (env var not set) counts as disabled."""
    token = get_config("api.token")
    if not token or str(token).startswith("${"):
        return None
    return str(token)


def _is_local(request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


async def api_token_guard(request, call_next):
    """ASGI middleware: reject /api/v1 requests without the shared token."""
    path = request.url.path
    if path.startswith("/api/v1") and not path.startswith(_EXEMPT_PREFIXES):
        token = configured_token()
        if token:
            allow_local = get_config("api.allow_localhost", True)
            bearer = _bearer(request) or ""
            authed = secrets.compare_digest(bearer, token) or (allow_local and _is_local(request))
            if not authed:
                return JSONResponse({"detail": "invalid or missing API token"}, status_code=401)
    return await call_next(request)
