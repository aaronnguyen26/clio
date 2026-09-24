#!/usr/bin/env python3
"""Autonomous Desktop Companion — Progressive Test Runner

Supports both standard pytest runner and zero-dependency standard library
unittest discovery. Allows filtering by test tier (Tier 1-4).
"""

import argparse
import os
import sys
import time
from typing import List


def get_tier_marker_expression(tiers: List[str]) -> str:
    """Builds a pytest marker expression for the given tiers."""
    markers = [f"tier{t.strip()}" for t in tiers if t.strip()]
    if not markers:
        return "tier1 or tier2 or tier3 or tier4"
    return " or ".join(markers)


def run_with_pytest(tier_expr: str, verbose: bool, extra_args: List[str]) -> int:
    """Runs tests via pytest module."""
    try:
        import pytest
    except ImportError:
        return -1

    cmd_args = ["tests", "-m", tier_expr]
    if verbose:
        cmd_args.append("-v")
    cmd_args.extend(extra_args)

    print(f"\n=======================================================")
    print(f"🚀 Running Test Suite via pytest: marker='{tier_expr}'")
    print(f"=======================================================\n")
    return pytest.main(cmd_args)


def run_with_unittest(tier_filter: List[str], verbose: bool) -> int:
    """Runs tests via standard library unittest discovery."""
    import unittest

    print(f"\n=======================================================")
    print(f"🚀 Running Test Suite via standard library unittest")
    print(f"=======================================================\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Discover tests in tests/
    discovered = loader.discover(start_dir="tests", pattern="test_*.py")

    # If tier filtering is requested, filter test classes/methods
    tier_markers = [f"tier{t.strip()}".lower() for t in tier_filter if t.strip()]

    def filter_suite(test_item):
        if isinstance(test_item, unittest.TestSuite):
            for sub_item in test_item:
                filter_suite(sub_item)
        else:
            # Check test method or class docstring/name for tier
            test_id = test_item.id().lower()
            if not tier_markers or any(m in test_id for m in tier_markers):
                suite.addTest(test_item)

    filter_suite(discovered)

    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Desktop Companion Test Runner")
    parser.add_argument(
        "--tier",
        type=str,
        default="1,2",
        help="Comma-separated tiers to run: 1, 2, 3, 4, or 'all' (default: 1,2)",
    )
    parser.add_argument(
        "--runner",
        choices=["auto", "pytest", "unittest"],
        default="auto",
        help="Test runner backend (default: auto)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    args, unknown = parser.parse_known_args()

    # Determine tiers
    if args.tier.lower() == "all":
        tiers = ["1", "2", "3", "4"]
    else:
        tiers = [t.strip() for t in args.tier.split(",") if t.strip()]

    start_time = time.time()
    exit_code = 0

    if args.runner == "unittest":
        exit_code = run_with_unittest(tiers, args.verbose)
    elif args.runner == "pytest":
        tier_expr = get_tier_marker_expression(tiers)
        res = run_with_pytest(tier_expr, args.verbose, unknown)
        if res == -1:
            print("ERROR: pytest is not installed in the current environment.")
            return 1
        exit_code = res
    else:  # auto
        tier_expr = get_tier_marker_expression(tiers)
        res = run_with_pytest(tier_expr, args.verbose, unknown)
        if res == -1:
            print("pytest not available, falling back to standard library unittest...")
            exit_code = run_with_unittest(tiers, args.verbose)
        else:
            exit_code = res

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed:.2f}s with status code {exit_code}.\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
