# unify-cli-web — Issues & Gotchas

- **SQLite `database is locked` under async concurrency**: `core.db.get_conn()` creates a new connection per call. When `run_pipeline` uses `asyncio.gather` with multiple coroutines writing simultaneously, connections conflict. Discovered while testing `create_and_run` and `resume_card`. Tests now mock `run_pipeline` to avoid this.
- **No `pytest-asyncio` dependency**: Async tests must use `asyncio.run()` directly. Not currently a blocker but good to know.


## F2: Code Quality Review (2026-07-04)

### Issue: Bare `except Exception:` in operations.py:325 (minor)
`_run_bg_async` has `except Exception:` with no logging. Swallows ALL exceptions including KeyboardInterrupt/SystemExit.
**Severity:** Low — card status is updated to 'failed', but the root cause is lost.
**Recommendation:** Add `logger.exception("Background task failed for card %s", card["id"])` before the status update.

### Observation: operations.py:348 — `except Exception: pass`
Has inline comment `# pgrep might fail — non-critical`. Acceptable given the non-critical cleanup context.

### Clean checks:
- No TODO/FIXME/HACK markers found in any modified file
- No unused imports
- No commented-out code
- No hardcoded paths (outside of resolve_pipeline_path which handles path resolution)
- All API routes delegate to operations.py for lifecycle operations
