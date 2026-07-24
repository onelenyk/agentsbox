import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("WORKSPACE_ROOT", str(_REPO_ROOT / "workspace"))

from fastapi import FastAPI
from web.routes import router
from web.api import api as api_router
from web.agent_mcp import agent_mcp
from core.scheduler import start_scheduler

app = FastAPI(title="Sisyphus", version="2.0")
app.include_router(router)
app.include_router(api_router)
app.include_router(agent_mcp)

start_scheduler()

if __name__ == "__main__":
    import uvicorn
    # Reload is OFF by default: across a long session with many edits, uvicorn's
    # auto-reload drifts from disk (partial reloads leave the worker running a
    # mix of old/new code), which silently produces behavior that no longer
    # matches the source. Opt in with SISYPHUS_RELOAD=1 for a quick edit loop;
    # otherwise restart the process to pick up changes.
    reload = os.environ.get("SISYPHUS_RELOAD") == "1"
    uvicorn.run("web.app:app", host="0.0.0.0", port=7677, reload=reload)
