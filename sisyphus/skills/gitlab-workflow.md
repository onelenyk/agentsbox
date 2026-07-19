# GitLab Workflow — Plan Mode

## Purpose
Analyze GitLab repositories in plan mode. Read code, create Fix Plans. NEVER commit or push in plan mode.

## Workflow
1. **FIRST**: `git checkout main && git pull`
2. **Phase 1**: grep + glob — instant search for patterns
3. **Phase 2**: read — read key files to understand patterns
4. **Phase 3**: Cross-reference — check if the code matches the issue claims
5. **Phase 4**: Create Fix Plan

## Output
Write TWO files to SISYPHUS_OUTPUT_DIR:

1. **result.json**:
```json
{
  "status": "completed",
  "summary": "Fix Plan summary (1 sentence)",
  "risk": "low"
}
```

2. **analysis.md** — detailed Fix Plan:
```markdown
## What the code already has
(existing patterns, implementations)

## Root cause
(why the issue exists)

## Discrepancies Found
(differences between issue and codebase)

## Files to change
| # | File | Action | Line |
|---|------|--------|------|

## Approach
(how to implement the fix)

## Risk
(low / medium / high with reasoning)
```

## Rules
- PLAN MODE ONLY — no commits, no pushes, no MRs
- Use grep + read BEFORE any exploration
- Always work on main branch
- Save raw grep results as resources
