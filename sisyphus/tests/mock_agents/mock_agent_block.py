#!/usr/bin/env python3
"""Mock agent that emits no output and never exits on its own.

Used by the launcher timeout tests: because the process blocks forever, the
only way launch_agent returns is via its own total/idle-timeout machinery (it
kills the process). There is no sleep duration that could race the timeout
clock, so the timeout assertions are deterministic.
"""
import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.parse_args()

while True:
    time.sleep(3600)
