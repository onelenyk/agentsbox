#!/usr/bin/env python3
"""Mock engineer agent — writes valid result.json + analysis.md."""
import argparse, json, os

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.add_argument("--status", default="completed")
args = parser.parse_args()

os.makedirs(args.output_dir, exist_ok=True)

result = {"status": args.status, "summary": "Mock engineer analysis: found 6 issues", "risk": "low"}
with open(os.path.join(args.output_dir, "result.json"), "w") as f:
    json.dump(result, f)

with open(os.path.join(args.output_dir, "analysis.md"), "w") as f:
    f.write("## Mock Engineer Analysis\nMock analysis content.\n")
