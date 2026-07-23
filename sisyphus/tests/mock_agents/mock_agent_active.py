#!/usr/bin/env python3
"""Mock agent that periodically prints output, simulating slow-but-alive progress."""
import json
import os
import time, argparse

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.add_argument("--ticks", type=int, default=5)
parser.add_argument("--interval", type=float, default=0.3)
args = parser.parse_args()

for i in range(args.ticks):
    print(f"tick {i}", flush=True)
    time.sleep(args.interval)

os.makedirs(args.output_dir, exist_ok=True)
with open(os.path.join(args.output_dir, "result.json"), "w") as f:
    json.dump({"status": "completed", "summary": f"ticked {args.ticks} times"}, f)
