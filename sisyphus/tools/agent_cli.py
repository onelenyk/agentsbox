#!/usr/bin/env python3
"""Agent toolbelt CLI — the only sanctioned way for an agent to talk back.

Invoked by agents via bash inside their sandbox ($SISYPHUS_AGENT_TOOL).
Writes ONLY into $SISYPHUS_OUTPUT_DIR; never touches the database — the
orchestrator ingests these files (events.jsonl live, result.json post-step).

Stdlib only: agents run with cwd=worktree and no access to sisyphus imports.

usage:
  agent_cli.py comment TEXT [--resource FILENAME]...
  agent_cli.py finish --status {completed,failed} --summary TEXT
               [--verdict {approved,changes_requested}] [--risk {low,medium,high}]
               [--error TEXT]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Keep in sync with core/schemas.py::BaseResult (drift-checked by
# tests/test_agent_cli.py::test_choices_match_base_result_schema).
VALID_STATUSES = ("completed", "failed")
VALID_VERDICTS = ("approved", "changes_requested")
VALID_RISKS = ("low", "medium", "high")


def _output_dir() -> Path:
    raw = os.environ.get("SISYPHUS_OUTPUT_DIR", "")
    if not raw:
        print("agent_cli: SISYPHUS_OUTPUT_DIR is not set — are you running inside a Sisyphus step?", file=sys.stderr)
        sys.exit(2)
    path = Path(raw)
    if not path.is_dir():
        print(f"agent_cli: SISYPHUS_OUTPUT_DIR does not exist: {raw}", file=sys.stderr)
        sys.exit(2)
    return path


def cmd_comment(args) -> int:
    out_dir = _output_dir()
    resources = []
    for r in args.resource or []:
        if "/" in r or "\\" in r:
            print(f"agent_cli: --resource must be a bare filename (got {r!r})", file=sys.stderr)
            return 2
        if not (out_dir / r).exists():
            print(f"agent_cli: warning: resource {r!r} does not exist (yet) in output dir", file=sys.stderr)
        resources.append(r)

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "comment",
        "text": args.text,
        "resources": resources,
    }
    # Single write() on an append-mode handle: O_APPEND keeps concurrent
    # appends line-atomic, so the orchestrator's tail never sees torn lines
    # from separate invocations.
    with open(out_dir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0


def cmd_finish(args) -> int:
    out_dir = _output_dir()
    if not args.summary.strip():
        print("agent_cli: --summary must not be empty", file=sys.stderr)
        return 2

    result = {"status": args.status, "summary": args.summary}
    if args.verdict:
        result["verdict"] = args.verdict
    if args.risk:
        result["risk"] = args.risk
    if args.error:
        result["error"] = args.error

    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent_cli.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_comment = sub.add_parser("comment", help="post a progress comment to the card's activity timeline")
    p_comment.add_argument("text")
    p_comment.add_argument("--resource", action="append", help="attach a file from the output dir (repeatable)")
    p_comment.set_defaults(func=cmd_comment)

    p_finish = sub.add_parser("finish", help="report the final result of this step (mandatory last action)")
    p_finish.add_argument("--status", required=True, choices=VALID_STATUSES)
    p_finish.add_argument("--summary", required=True)
    p_finish.add_argument("--verdict", choices=VALID_VERDICTS)
    p_finish.add_argument("--risk", choices=VALID_RISKS)
    p_finish.add_argument("--error")
    p_finish.set_defaults(func=cmd_finish)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
