## Learnings

### TASK 4-6: Git repository integration pipeline

- **TASK 4** (`pipeline.py`): Added auto-resolve logic before `if gitlab_repo_url:` — when no URL is provided, the pipeline reads `card["platforms"]`, extracts the first platform, and calls `get_repo_by_platform()` to find the matching repo from `config.yaml`.
- **TASK 5A** (`new_card.html`): Replaced the free-text platforms input with a `<select>` dropdown (android/ios/kmp). Added HTMX `hx-get="/api/repos/resolve"` with `hx-target="#repo-hint"` to show which repo the platform resolves to.
- **TASK 5B** (`routes.py`): Added `GET /api/repos/resolve?platform=android` endpoint — returns "Repo: name (branch)" or "No repo configured for this platform".
- **TASK 5C** (`card_detail.html`): Added conditional `{% if repo_name %}` info row showing the resolved repo name.
- **TASK 5D** (`routes.py` card_detail route): Added repo resolution from `card["platforms"]` using `get_repo_by_platform()`, passed `repo_name` to template.
- **TASK 6A**: Verified Fetch button already posts to `/api/repos/{name}/update`.
- **TASK 6B**: Added `GET /api/repos/health` — returns `{"total", "cloned", "pending"}` counts.

### Gotchas
- The `?` in curl URLs needs quoting in zsh: `"http://...?platform=android"` not bare.
- `card["platforms"]` stored as JSON string in DB; must `json.loads()` before using.
- No LSP (basedpyright) available in this env — used `python3 -m py_compile` for syntax validation.
