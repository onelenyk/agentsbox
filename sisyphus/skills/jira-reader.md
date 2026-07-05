# Jira Reader

## Purpose
Fetch and analyze Jira issues. Gather all linked issues for full context.

## Workflow
1. Use the `fetch_jira` tool to retrieve the Jira issue data (already in resources/)
2. Read the Jira data from resources/ directory
3. Cross-reference issue details with linked issues
4. Analyze screenshots (if present — use describe_image tool output in resources/)

## Output
Write TWO files to SISYPHUS_OUTPUT_DIR:

1. **result.json** — machine-readable summary:
```json
{
  "status": "completed",
  "summary": "Jira issue analysis summary (1 sentence)",
  "risk": "low",
  "platforms": ["android"]
}
```

2. **analysis.md** — detailed Jira analysis in markdown:
```markdown
## Jira Source
(key, type, priority, status, reporter)

## Issue Details
(summary, description)

## Linked Issues
(list all linked issues with keys)

## Screenshots
(summary of screenshot descriptions from resources/)

## Acceptance Criteria
(checklist from Jira issue)
```

## Rules
- ALWAYS check for linked issues — they provide critical context
- NEVER modify the Jira data files in resources/ — they are append-only
- If screenshots are available, read the describe_image tool output
