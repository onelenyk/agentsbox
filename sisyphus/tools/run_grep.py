#!/usr/bin/env python3
"""Run grep in a directory and output results. Caches results."""
import os
import subprocess
import sys

def run_grep(pattern: str, directory: str, include: str = None) -> str:
    if not os.path.isdir(directory):
        print(f"ERROR: Directory not found: {directory}")
        sys.exit(1)

    cmd = ["grep", "-rn", "--color=never"]
    if include:
        cmd.extend(["--include", include])
    cmd.extend([pattern, directory])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.stdout if result.stdout else "(no matches found)"
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out after 30s"
    except FileNotFoundError:
        return "ERROR: grep command not found"

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python tools/run_grep.py PATTERN DIRECTORY [--include GLOB]")
        print("Example: python tools/run_grep.py BrandManager /workspace/repos/android-driver --include '*.kt'")
        sys.exit(0)

    inc = None
    args = sys.argv[1:]
    if "--include" in args:
        idx = args.index("--include")
        inc = args[idx + 1]
        args = args[:idx]

    output = run_grep(args[0], args[1], inc)
    print(output)
