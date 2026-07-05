# Sisyphus — Git Repository Management

## TL;DR

> **Quick Summary**: Add unified Git repository management — add local or remote repos, auto-clone to bare clones, platform-to-repo mapping, worktree lifecycle. System never works with original source, only bare clones.
>
> **Deliverables**:
> - `core/repos.py` — repo manager (add, clone, list, map platform→repo)
> - `config.yaml` — `repos:` section with name, source, platform, branch
> - UI: Repos page — CRUD for repositories
> - Integration: pipeline auto-selects repo by platform
> - Integration: new card form auto-resolves repo
>
> **Estimated Effort**: ~4 hours (6 tasks)
> **Parallel Execution**: YES — 2 waves

---

## Context

### Problem
- Pipeline requires `gitlab_repo_url` passed manually — no repo registry
- Web UI can't auto-select repo for a given platform
- No way to manage which repos are available
- Worktree creation works but no lifecycle management

### Solution
- Single source of truth: bare clones at `/workspace/repos/{name}/`
- Repos registered in `config.yaml` with platform mapping
- On add: clone immediately (local or remote)
- On pipeline start: platform → repo → worktree (automatic)

---

## Architecture

```
config.yaml                    /workspace/repos/
┌──────────────────┐          ┌──────────────────────┐
│ repos:           │          │ android-driver/      │ ← bare clone
│  - name: android │ clone    │   objects/           │
│    platform: andr │────────→│   refs/              │
│    source: /local │          │   worktrees/         │
│    branch: Staging│          │     run-001/ ← агент │
│                  │          │                      │
│  - name: kmp     │          │ kmp-driver/          │
│    platform: kmp  │          │   ...               │
│    source: git@.. │          │                      │
│    branch: main   │          └──────────────────────┘
└──────────────────┘
```

---

## Work Objectives

### Must Have
- Config-driven repo registry (`config.yaml` → `repos:`)
- `core/repos.py` — add, clone, list, get_by_platform, update
- Auto-clone on add (local path OR remote URL)
- UI page: list repos, add new, view details
- Pipeline: platform → repo → worktree (automatic)
- Branch selection per repo

### Must NOT Have
- NO auto-push/commit to remote (execution mode — v3)
- NO multi-branch worktree per run (single branch per run)
- NO submodule support
- NO LFS support

---

## Tasks

### Wave 1 — Core (3 tasks, ~2h)

- [ ] 1. Config schema + core/repos.py

  **What**: Add `repos:` section to `config.yaml`, create repo manager.
  
  **config.yaml** — add example:
  ```yaml
  repos:
    - name: android-driver
      platform: android
      source: /Users/lenyk/work/android-driver
      branch: Staging
  ```
  
  **core/repos.py**:
  ```python
  """Git repository manager — bare clones + platform mapping."""
  import os, subprocess
  from pathlib import Path
  
  WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
  REPOS_DIR = Path(WORKSPACE_ROOT) / "repos"
  
  def load_repos() -> list[dict]:
      """Load repo configs from config.yaml."""
      from core.config import get_config
      return get_config("repos", [])
  
  def add_repo(name: str, source: str, platform: str, branch: str = "Staging"):
      """Add and clone a repo. Creates bare clone at REPOS_DIR/{name}/."""
      bare_path = REPOS_DIR / name
      if not bare_path.exists():
          result = subprocess.run(
              ["git", "clone", "--bare", source, str(bare_path)],
              capture_output=True, text=True
          )
          if result.returncode != 0:
              raise RuntimeError(f"Clone failed: {result.stderr}")
      # Save to config
      _save_repo_config(name, source, platform, branch)
      return str(bare_path)
  
  def get_repo(name: str) -> dict | None:
      """Get repo config by name."""
      for r in load_repos():
          if r["name"] == name:
              return r
      return None
  
  def get_repo_by_platform(platform: str) -> dict | None:
      """Find repo mapped to a platform."""
      for r in load_repos():
          if r.get("platform") == platform:
              return r
      return None
  
  def list_repos() -> list[dict]:
      """List all repos with clone status."""
      repos = load_repos()
      for r in repos:
          bare_path = REPOS_DIR / r["name"]
          r["cloned"] = bare_path.exists() and (bare_path / "HEAD").exists()
      return repos
  
  def update_repo(name: str):
      """Fetch latest changes for a bare clone."""
      bare_path = REPOS_DIR / name
      if bare_path.exists():
          subprocess.run(["git", "-C", str(bare_path), "fetch", "origin"], capture_output=True)
  
  def _save_repo_config(name, source, platform, branch):
      """Append repo to config.yaml."""
      import yaml
      config_path = Path(__file__).parent.parent / "config.yaml"
      config = yaml.safe_load(config_path.read_text()) or {}
      repos = config.get("repos", [])
      # Remove existing entry if present
      repos = [r for r in repos if r.get("name") != name]
      repos.append({"name": name, "source": source, "platform": platform, "branch": branch})
      config["repos"] = repos
      config_path.write_text(yaml.dump(config, default_flow_style=False))
  ```
  
  **Acceptance**: `from core.repos import list_repos; print(list_repos())` works

- [ ] 2. UI — Repos page

  **What**: Add `/repos` page with list of repos, add form.
  
  **File: `sisyphus/web/templates/repos.html`**:
  ```html
  {% extends "base.html" %}
  {% block content %}
  <div class="header-bar">
      <h1>Repositories</h1>
      <button class="btn btn-primary" hx-get="/api/repos/new-form" hx-target="#modal-container">+ Add Repo</button>
  </div>
  <div id="repos-list" hx-get="/api/repos/list" hx-trigger="load, every 10s">
      <div class="empty-state"><p>Loading...</p></div>
  </div>
  {% endblock %}
  ```
  
  **File: `sisyphus/web/templates/repos_list.html`** — partial for HTMX:
  ```html
  {% for r in repos %}
  <div class="agent-card">
      <div class="agent-header">
          <div>
              <div class="agent-name">{{ r.name }}</div>
              <div class="agent-role">{{ r.source[:50] }}</div>
          </div>
          <span class="access-badge {{ 'analyze' if r.cloned else 'read' }}">{{ 'cloned' if r.cloned else 'pending' }}</span>
      </div>
      <div class="agent-meta">
          Platform: <span>{{ r.platform }}</span> &nbsp;|&nbsp;
          Branch: <span>{{ r.branch }}</span>
      </div>
  </div>
  {% endfor %}
  ```
  
  **File: `sisyphus/web/templates/repos_new.html`** — modal form:
  ```html
  <div class="modal-overlay">
      <div class="modal">
          <h2>Add Repository</h2>
          <form hx-post="/api/repos" hx-target="#modal-container" hx-swap="outerHTML">
              <div class="form-group">
                  <label>Name</label>
                  <input type="text" name="name" placeholder="android-driver" required>
              </div>
              <div class="form-group">
                  <label>Source (local path or git URL)</label>
                  <input type="text" name="source" placeholder="/Users/... or git@github.com:..." required>
              </div>
              <div class="form-group">
                  <label>Platform</label>
                  <select name="platform">
                      <option value="android">Android</option>
                      <option value="ios">iOS</option>
                      <option value="kmp">KMP</option>
                  </select>
              </div>
              <div class="form-group">
                  <label>Branch</label>
                  <input type="text" name="branch" value="Staging">
              </div>
              <div class="form-actions">
                  <button type="button" class="btn" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                  <button type="submit" class="btn btn-primary">Clone & Add</button>
              </div>
          </form>
      </div>
  </div>
  ```
  
  **Routes to add in `sisyphus/web/routes.py`**:
  ```python
  @router.get("/repos", response_class=HTMLResponse)
  async def repos_page(request: Request):
      return _tmpl().TemplateResponse("repos.html", {"request": request, "active_page": "repos"})
  
  @router.get("/api/repos/list", response_class=HTMLResponse)
  async def repos_list(request: Request):
      from core.repos import list_repos
      repos = list_repos()
      return _tmpl().TemplateResponse("repos_list.html", {"request": request, "repos": repos})
  
  @router.get("/api/repos/new-form", response_class=HTMLResponse)
  async def repos_new_form(request: Request):
      return _tmpl().TemplateResponse("repos_new.html", {"request": request})
  
  @router.post("/api/repos")
  async def repos_add(name: str = Form(...), source: str = Form(...), 
                      platform: str = Form(...), branch: str = Form("Staging")):
      from core.repos import add_repo
      add_repo(name, source, platform, branch)
      return {"status": "ok", "redirect": "/repos"}
  ```
  
  **Acceptance**: Navigate to `/repos`, add a repo, see it appear in list

- [ ] 3. Nav + route integration

  **What**: Add "Repos" link to base.html nav, register in routes.py.
  
  **base.html nav** — add:
  ```html
  <a href="/repos" class="{{ 'active' if active_page == 'repos' else '' }}">Repos</a>
  ```

### Wave 2 — Pipeline Integration (3 tasks, ~2h)

- [ ] 4. Auto-resolve repo in pipeline

  **What**: Update `run_pipeline` in `core/pipeline.py` to auto-resolve repo by platform when no `gitlab_repo_url` is provided.
  
  ```python
  # In run_pipeline(), after creating the run:
  if not gitlab_repo_url:
      from core.repos import get_repo_by_platform
      repo = get_repo_by_platform(card.get("platforms", ["android"])[0])
      if repo:
          gitlab_repo_url = repo["source"]
          branch = repo.get("branch", "Staging")
  ```
  
  **Acceptance**: Create card with platform=android → pipeline finds android-driver repo automatically

- [ ] 5. Platform-to-repo mapping in card creation

  **What**: When creating a card (CLI or Web), platform selection auto-resolves to a repo. Show selected repo in card detail.
  
  **Update `create_card`** in `core/state.py` — store resolved repo name in card metadata.
  
  **Update card_detail.html** — show repo name if available.
  
  **Acceptance**: Card created with platform=android → card shows repo "android-driver"

- [ ] 6. Repo health check + update

  **What**: Add "Update" button on repos page to fetch latest. Show last fetch time. Periodic update via scheduler.
  
  **API**: `POST /api/repos/{name}/update` → calls `update_repo(name)`
  
  **Acceptance**: Click Update → bare clone fetches new commits from origin

---

## Commit Strategy

| Task | Message |
|------|---------|
| 1 | `feat(repos): core repo manager with config persistence` |
| 2 | `feat(repos): UI page for repo CRUD` |
| 3 | `feat(repos): nav link and route registration` |
| 4 | `feat(repos): auto-resolve repo from platform in pipeline` |
| 5 | `feat(repos): platform-to-repo mapping in card creation` |
| 6 | `feat(repos): repo update/fetch + health check` |
