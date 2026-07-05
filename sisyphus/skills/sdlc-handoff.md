# SDLC Handoff — Output Templates

## Purpose
Standardized output format for each role in the SDLC pipeline.

## Templates by Role

### PM
```
## Jira Source | ## Branch: Staging | ## Issue Details | ## Screenshots | ## Acceptance Criteria
```

### Tech Lead
```
## Technical Analysis | ## Platform Assignment | ## Affected Components
```

### Engineer
```
## Fix Plan | ### Files | ### Approach | ### Risk
```

### Code Reviewer
```
## Code Review | Status: APPROVED/CHANGES_REQUESTED
```

Reviewer MUST set verdict in result.json:
- `"verdict": "approved"` — plan is complete and correct
- `"verdict": "changes_requested"` — plan needs revision (explain what specifically)

### QA
```
## QA Validation | Status: PASS/FAIL
```

## Output Format
ALL roles write TWO files to SISYPHUS_OUTPUT_DIR:
1. result.json — machine-readable metadata (status, verdict, summary, risk)
2. analysis.md — detailed analysis in markdown

## Handoff
When your step is complete, the orchestrator automatically advances to the next step. 
You do NOT need to specify who comes next — the pipeline YAML defines the sequence.
