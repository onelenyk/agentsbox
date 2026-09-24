#!/usr/bin/env python3
"""Smoke test runners with read_isolation=True (forced via monkeypatch).

Run from sisyphus/ dir with: WORKSPACE_ROOT=$PWD/workspace python3 smoke_test_with_isolation.py
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

    # Now import runner_smoke and runner_profiles
    from core.runner_smoke import smoke_test
    from core import runner_profiles
    import shutil

    os.environ["WORKSPACE_ROOT"] = os.path.join(os.getcwd(), "workspace")

    results = {}

    # Test claude runner
    print("=" * 80)
    print("Testing claude runner with read_isolation=True")
    print("=" * 80)

    claude_path = shutil.which("claude")
    if not claude_path:
        print("WARNING: claude binary not found, skipping claude test")
        result_claude = {
            "ok": False,
            "stage": "binary",
            "detail": "claude binary not found",
            "runner": "claude-cli"
        }
    else:
        claude_runner = {
            "name": "claude",
            "type": "claude-cli",
            "binary": claude_path,
            "model": "claude-opus",
            "extra_args": "[]"
        }

        try:
            result_claude = await smoke_test(claude_runner, timeout=120)
        except Exception as e:
            result_claude = {
                "ok": False,
                "stage": "error",
                "detail": str(e),
                "runner": "claude-cli"
            }

    results["claude"] = result_claude
    print(f"Result: {result_claude}")
    print()

    # Test opencode runner
    print("=" * 80)
    print("Testing opencode runner with read_isolation=True")
    print("=" * 80)

    opencode_path = shutil.which("opencode")
    if not opencode_path:
        print("WARNING: opencode binary not found, skipping opencode test")
        result_opencode = {
            "ok": False,
            "stage": "binary",
            "detail": "opencode binary not found",
            "runner": "opencode"
        }
    else:
        opencode_runner = {
            "name": "opencode-default",
            "type": "opencode",
            "binary": opencode_path,
            "model": "gpt-4o",
            "extra_args": "[]"
        }

        try:
            result_opencode = await smoke_test(opencode_runner, timeout=120)
        except Exception as e:
            result_opencode = {
                "ok": False,
                "stage": "error",
                "detail": str(e),
                "runner": "opencode"
            }

    results["opencode"] = result_opencode
    print(f"Result: {result_opencode}")
    print()

    # Summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    for name, result in results.items():
        status = "PASS" if result.get("ok") else "FAIL"
        print(f"{name}: {status}")
        if not result.get("ok"):
            print(f"  Stage: {result.get('stage')}")
            detail = result.get('detail', '')
            if len(detail) > 200:
                detail = detail[:200] + "..."
            print(f"  Detail: {detail}")

    # Return exit code based on results
    all_passed = all(r.get("ok") for r in results.values())
    return 0 if all_passed else 1

if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    sys.exit(exit_code)
