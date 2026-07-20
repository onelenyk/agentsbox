import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("WORKSPACE_ROOT", str(_REPO_ROOT / "workspace"))

from fastapi import FastAPI
from web.routes import router
from core.scheduler import start_scheduler

app = FastAPI(title="Sisyphus", version="2.0")
app.include_router(router)

start_scheduler()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.app:app", host="0.0.0.0", port=7677, reload=True)
