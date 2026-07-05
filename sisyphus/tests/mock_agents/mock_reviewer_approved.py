#!/usr/bin/env python3
import argparse, json, os
parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
args = parser.parse_args()
os.makedirs(args.output_dir, exist_ok=True)
result = {"status": "completed", "verdict": "approved", "summary": "Mock reviewer: plan approved"}
with open(os.path.join(args.output_dir, "result.json"), "w") as f:
    json.dump(result, f)
