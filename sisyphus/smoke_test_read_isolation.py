#!/usr/bin/env python3
"""Smoke test runners with read_isolation=True (forced via monkeypatch).

Run from sisyphus/ dir with: WORKSPACE_ROOT=$PWD/workspace python3 smoke_test_read_isolation.py
"""
import asyncio
import os
import sys
from unittest.mock import patch

async def run_tests():
    # Patch get_config to force read_isolation=True before any imports that use it
    from core import config
    original_get_config = config.get_config

    def patched_get_config(key=None, default=None):
        """Monkeypatched get_config that forces sandbox.read_isolation to True."""
        if key == "sandbox.read_isolation":
            return True
        return original_get_config(key, default)

    config.get_config = patched_get_config

    # Now import runner_smoke
    from core.runner_smoke import smoke_test_by_name

    os.environ["WORKSPACE_ROOT"] = os.path.join(os.getcwd(), "workspace")

    print("=" * 80)
    print("Testing claude runner with read_isolation=True")
    print("=" * 80)

    try:
        result_claude = await smoke_test_by_name("claude", timeout=120)
    except Exception as e:
        result_claude = {
            "ok": False,
            "stage": "error",
            "detail": str(e),
            "runner": "claude"
        }

    print(f"Result: {result_claude}")
    print()

    print("=" * 80)
    print("Testing opencode-default runner with read_isolation=True")
    print("=" * 80)

    try:
        result_opencode = await smoke_test_by_name("opencode-default", timeout=120)
    except Exception as e:
        result_opencode = {
            "ok": False,
            "stage": "error",
            "detail": str(e),
            "runner": "opencode-default"
        }

    print(f"Result: {result_opencode}")
    print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Claude: {'PASS' if result_claude.get('ok') else 'FAIL'}")
    if not result_claude.get('ok'):
        print(f"  Stage: {result_claude.get('stage')}")
        print(f"  Detail: {result_claude.get('detail', '')[:200]}")
    print()
    print(f"OpenCode: {'PASS' if result_opencode.get('ok') else 'FAIL'}")
    if not result_opencode.get('ok'):
        print(f"  Stage: {result_opencode.get('stage')}")
        print(f"  Detail: {result_opencode.get('detail', '')[:200]}")

    # Return exit code based on results
    if result_claude.get('ok') and result_opencode.get('ok'):
        return 0
    return 1

if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    sys.exit(exit_code)
