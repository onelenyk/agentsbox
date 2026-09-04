import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env before anything reads the environment (config, DB path, secrets).
from core.env import load_env
load_env()
os.environ.setdefault("WORKSPACE_ROOT", str(_REPO_ROOT / "workspace"))

from core.logging_setup import setup_logging
setup_logging()

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from web.routes import router, startup, _tmpl
from web.api import api as api_router
from web.agent_mcp import agent_mcp
from web.auth import api_token_guard, _wants_html



@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup()
    yield


app = FastAPI(title="Sisyphus", version="2.0", lifespan=lifespan)
app.middleware("http")(api_token_guard)
app.mount("/static", StaticFiles(directory=str(_REPO_ROOT / "web" / "static")), name="static")
app.include_router(router)
app.include_router(api_router)
app.include_router(agent_mcp)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Give a mistyped URL a way back instead of a raw JSON dead end.

    Only a page navigation that stays out of /api and asks for text/html gets
    the styled not_found page. Every API/JSON caller — including 404s thrown
    from inside /api/v1 routes with their own detail text — falls through to
    FastAPI's default handler, so the JSON body is byte-for-byte as before.
    """
    if exc.status_code == 404 and not request.url.path.startswith("/api") and _wants_html(request):
        return _tmpl().TemplateResponse(request, "not_found.html",
                                        {"active_page": "", "path": request.url.path}, status_code=404)
    return await http_exception_handler(request, exc)

if __name__ == "__main__":
    import uvicorn
    # Reload is OFF by default: across a long session with many edits, uvicorn's
    # auto-reload drifts from disk (partial reloads leave the worker running a
    # mix of old/new code), which silently produces behavior that no longer
    # matches the source. Opt in with SISYPHUS_RELOAD=1 for a quick edit loop;
    # otherwise restart the process to pick up changes.
    reload = os.environ.get("SISYPHUS_RELOAD") == "1"
    host = os.environ.get("SISYPHUS_HOST", "127.0.0.1")
    uvicorn.run("web.app:app", host=host, port=7677, reload=reload)
