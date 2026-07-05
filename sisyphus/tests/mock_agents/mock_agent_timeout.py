#!/usr/bin/env python3
"""Mock agent that simulates a timeout by sleeping."""
import time, argparse
parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.add_argument("--sleep", type=int, default=30)
args = parser.parse_args()
time.sleep(args.sleep)
