#!/usr/bin/env python3
"""Tests for runner/promote.py CLI argument handling.

Invoked as `python -m runner.promote` per the runner package convention, so these
exercise the real entry point via subprocess.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_promote(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "runner.promote", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class PromoteCliTests(unittest.TestCase):
    def test_help_flag_short_circuits_with_exit_zero(self):
        # Regression: --help used to be parsed as the slug and fail (T-2026-238).
        res = run_promote("--help")
        self.assertEqual(res.returncode, 0)
        self.assertIn("usage:", res.stdout)

    def test_missing_slug_exits_nonzero(self):
        res = run_promote()
        self.assertNotEqual(res.returncode, 0)

    def test_path_separator_slug_rejected(self):
        res = run_promote("foo/bar")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid slug", res.stderr)


if __name__ == "__main__":
    unittest.main()
