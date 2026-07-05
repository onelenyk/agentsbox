#!/usr/bin/env python3
"""Mock agent that writes invalid JSON."""
import argparse, os
parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
args = parser.parse_args()
os.makedirs(args.output_dir, exist_ok=True)
with open(os.path.join(args.output_dir, "result.json"), "w") as f:
    f.write('{"garbage": true, invalid json here}')
