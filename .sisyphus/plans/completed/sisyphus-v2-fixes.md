# Sisyphus v2 — UI Bug Fixes & Data Completion

## TL;DR

> **Quick Summary**: Fix CSS gaps (undefined classes), add missing data fields to all pages, improve visual hierarchy, fix navigation active states. ~4 hours, 8 tasks.

---

## Issues Found

### CSS (Critical — pages render but look broken)
- `.info-row`, `.label`, `.value` — undefined (used in card_detail, runtime)
- `.header-bar`, `.kanban-grid`, `.kanban-col`, `.count` — undefined (kanban)
- `.pipeline-tag`, `.badge` — undefined (cards)
- `.steps-table` — undefined (card detail)
- `.empty-state` — defined but no styles
- Status badges (not just dots) — missing for cards

### Missing Data (Medium — data exists in DB but not shown)
- Kanban cards: no status badge, no agent name, no duration, no priority
- Card detail: no step durations, no log preview, resource content not shown
- Dashboard: no pipeline breakdown, no success rate %
- Agents: no YAML config viewer (just summary)
- Runtime: config shown as text, not structured
- History: no filters (by status, pipeline, date)

### Navigation (Low)
- Active nav link not highlighted
- Page titles inconsistent
- No favicon

---

## Tasks

### Wave 1 — CSS Fixes (2 tasks, ~1h)

- [ ] 1. Complete base.html CSS

  **What**: Add ALL missing CSS classes to `web/templates/base.html`:
  
  ```css
  /* Header bar */
  .header-bar { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; }
  
  /* Kanban grid */
  .kanban-grid { display:grid; grid-template-columns:repeat(4, 1fr); gap:16px; }
  .kanban-col { background:#161b22; border:1px solid #30363d; border-radius:8px; min-height:300px; display:flex; flex-direction:column; }
  .kanban-col h2 { padding:12px 16px; font-size:13px; font-weight:600; color:#f0f6fc; border-bottom:1px solid #21262d; display:flex; justify-content:space-between; }
  .count { background:#21262d; padding:1px 8px; border-radius:12px; font-size:11px; }
  
  /* Cards */
  .card .title { font-size:13px; color:#c9d1d9; margin-bottom:8px; line-height:1.4; }
  .card .meta { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .pipeline-tag { background:#1f3a5f; color:#58a6ff; padding:1px 6px; border-radius:4px; font-size:10px; font-weight:500; }
  .badge { background:#21262d; color:#8b949e; padding:1px 6px; border-radius:4px; font-size:10px; }
  .status-badge { padding:2px 8px; border-radius:4px; font-size:10px; font-weight:600; text-transform:uppercase; }
  .status-badge.completed { background:#1b3a2d; color:#3fb950; }
  .status-badge.running { background:#1f3a5f; color:#58a6ff; }
  .status-badge.failed { background:#3d1f1f; color:#f85149; }
  .status-badge.created, .status-badge.pending { background:#21262d; color:#8b949e; }
  
  /* Detail panel */
  .info-row { display:flex; padding:6px 0; border-bottom:1px solid #21262d; font-size:13px; }
  .info-row .label { color:#8b949e; min-width:100px; }
  .info-row .value { color:#c9d1d9; }
  
  /* Steps table */
  .steps-table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  .steps-table th { text-align:left; padding:6px 8px; color:#8b949e; border-bottom:1px solid #21262d; font-weight:500; }
  .steps-table td { padding:6px 8px; border-bottom:1px solid #21262d; }
  
  /* Resource item */
  .resource-item { padding:6px 8px; border-bottom:1px solid #21262d; font-size:12px; display:flex; justify-content:space-between; }
  .resource-item a { color:#58a6ff; text-decoration:none; }
  .resource-item a:hover { text-decoration:underline; }
  
  /* Empty state */
  .empty-state { text-align:center; padding:40px 20px; color:#8b949e; }
  .empty-state p { font-size:14px; margin-bottom:8px; }
  
  /* Mermaid container */
  .mermaid-container { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:16px; margin:12px 0; }
  
  /* Active nav */
  nav a.active { color:#f0f6fc !important; background:#1f3a5f; }
  
  /* Stat cards */
  .stat-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:20px; text-align:center; }
  .stat-card .stat-value { font-size:36px; font-weight:700; }
  .stat-card .stat-label { font-size:12px; color:#8b949e; margin-top:4px; text-transform:uppercase; letter-spacing:0.5px; }
  
  /* Agent card */
  .agent-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }
  .agent-card .agent-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
  .agent-card .agent-name { font-size:15px; font-weight:600; color:#f0f6fc; }
  .agent-card .agent-role { font-size:12px; color:#8b949e; margin-top:2px; }
  .agent-card .agent-meta { font-size:12px; color:#8b949e; }
  .agent-card .agent-meta span { color:#c9d1d9; }
  
  /* Access badge */
  .access-badge { padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
  .access-badge.read { background:#21262d; color:#8b949e; }
  .access-badge.analyze { background:#1f3a5f; color:#58a6ff; }
  .access-badge.edit { background:#3d1f1f; color:#f85149; }
  ```

- [ ] 2. Fix navigation active state + page titles

  **What**: Update all page templates to set title consistently. Fix `base.html` nav to highlight active page:
  
  In `base.html`, add to `<style>`:
  ```css
  nav a.active { color:#f0f6fc !important; background:#1f3a5f; }
  ```
  
  In each page template, extend base with proper title and pass `active_page` variable:
  ```html
  {% extends "base.html" %}
  {% block title %}Dashboard — Sisyphus{% endblock %}
  ```
  
  Update `base.html` nav to use `active_page`:
  ```html
  <a href="/kanban" class="{{ 'active' if active_page == 'kanban' else '' }}">Kanban</a>
  ```
  
  Update `routes.py` to pass `active_page` in every route's template context.

### Wave 2 — Data Enrichment (4 tasks, ~2h)

- [ ] 3. Enrich Kanban cards

  **What**: Add status badge, agent name, and compact metadata to each card:
  ```html
  <div class="card" hx-get="/api/cards/{{ card.id }}/detail" hx-target="#card-detail" 
       onclick="document.getElementById('cardDetailPanel').classList.add('active')">
    <div class="title">{{ card.description[:80] }}</div>
    <div class="meta">
      <span class="pipeline-tag">{{ card.pipeline_name }}</span>
      <span class="status-badge {{ card.status }}">{{ card.status }}</span>
      {% if card.current_agent %}
      <span class="badge">{{ card.current_agent }}</span>
      {% endif %}
    </div>
  </div>
  ```

  Update `/api/cards/board` route to JOIN with steps to get `current_agent`:
  ```python
  query = """
    SELECT c.*, s.agent_name as current_agent
    FROM cards c
    LEFT JOIN runs r ON r.card_id = c.id AND r.id = (
      SELECT id FROM runs WHERE card_id = c.id ORDER BY started_at DESC LIMIT 1
    )
    LEFT JOIN steps s ON s.run_id = r.id AND s.status IN ('running', 'pending')
    WHERE c.status = ?
    ORDER BY c.created_at DESC
  """
  ```

- [ ] 4. Enrich Card Detail

  **What**: Add step durations, log preview, resource content inline:
  
  - Steps: compute duration from started_at/completed_at
  - Resources: click filename → show content via `hx-get="/api/resources/{{ run.id }}/{{ r.filename }}"`
  - Add log preview tab
  
  Update `/api/cards/{card_id}/detail` route to include computed durations:
  ```python
  for s in steps:
      if s.get("started_at") and s.get("completed_at"):
          # compute duration
          pass
  ```

- [ ] 5. Enrich Dashboard

  **What**: Add pipeline breakdown + success rate %:
  ```html
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px">
    <div class="stat-card">
      <div class="stat-value" style="color:#58a6ff">{{ stats.total }}</div>
      <div class="stat-label">Total</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:#3fb950">{{ success_rate }}%</div>
      <div class="stat-label">Success Rate</div>
    </div>
    ...
  </div>
  ```

- [ ] 6. Enrich Agents & Runtime

  **What**: 
  - Agents: show full YAML config in expandable section
  - Runtime: show config.yaml in structured format, not raw text
  
  Agents page: add expand/collapse for YAML config:
  ```html
  <details style="margin-top:8px">
    <summary style="cursor:pointer;color:#58a6ff;font-size:12px">View Config</summary>
    <pre style="font-size:11px;color:#8b949e;margin-top:8px;background:#0d1117;padding:12px;border-radius:6px;overflow-x:auto">{{ a|tojson(indent=2) }}</pre>
  </details>
  ```
  
  Note: Jinja2's `tojson` filter requires `json` import in routes. Alternative: use `pformat`.

### Wave 3 — Polish (2 tasks, ~1h)

- [ ] 7. Add search/filter to History page

  **What**: Filter runs by status, pipeline, date:
  ```html
  <div class="header-bar">
    <h1>History</h1>
    <select hx-get="/api/history/filter" hx-target="#history-table" hx-trigger="change" name="status">
      <option value="">All</option>
      <option value="completed">Completed</option>
      <option value="failed">Failed</option>
      <option value="running">Running</option>
    </select>
  </div>
  ```

- [ ] 8. Add favicon + loading states + error boundaries

  **What**: 
  - Dark-mode SVG favicon
  - Loading spinner between page transitions
  - Error boundary: friendly message on API failure
  - Auto-dismiss success/error toasts

---

## Commit Strategy

| Task | Message |
|------|---------|
| 1 | `fix(ui): add missing CSS classes for kanban, detail, agents` |
| 2 | `fix(ui): fix nav active state and page titles` |
| 3 | `feat(ui): enrich kanban cards with status, agent, metadata` |
| 4 | `feat(ui): enrich card detail with durations, resources, logs` |
| 5 | `feat(ui): enrich dashboard with success rate, pipeline breakdown` |
| 6 | `feat(ui): enrich agents with YAML config viewer` |
| 7 | `feat(ui): add filters to history page` |
| 8 | `feat(ui): add favicon, loading states, error boundaries` |
