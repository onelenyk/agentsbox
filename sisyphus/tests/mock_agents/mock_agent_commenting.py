#!/usr/bin/env python3
"""Mock agent that posts comments via the toolbelt CLI while it works.

Reads SISYPHUS_OUTPUT_DIR / SISYPHUS_AGENT_TOOL from env and ignores argv
entirely, so it can be registered directly as a runner binary (the launcher's
`<binary> run <prompt> --format json --auto` argv layout is irrelevant).
"""
import os
import subprocess
import sys
import time

OUT = os.environ["SISYPHUS_OUTPUT_DIR"]
TOOL = os.environ["SISYPHUS_AGENT_TOOL"]


def tool(*args):
    subprocess.run([sys.executable, TOOL, *args], check=True)


with open(os.path.join(OUT, "notes.md"), "w") as f:
    f.write("# Findings\n")

for i in range(3):
    print(f"working on step {i}", flush=True)
    tool("comment", f"milestone {i} reached", "--resource", "notes.md")
    time.sleep(1)

tool("finish", "--status", "completed", "--summary", "posted 3 comments")
