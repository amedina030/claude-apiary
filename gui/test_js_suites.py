"""Run the frontend's Node test suites from pytest.

`gui/web/*.js` holds the pure, browser-free modules extracted out of app.js
(prompt detection, bubble-anomaly classification, message reconciliation, the
thinking-bubble state machine). Each has a `node:test` suite next to it, and
until this file existed those suites were wired into nothing — no pytest
bridge, no script, no CI — so they only ran when somebody remembered to type
`node gui/web/test_*.js`. This makes `pytest gui` run them too.

Node is not a hard dependency of the repo: when it isn't on PATH the suites
SKIP rather than fail, so a Python-only checkout still gets a green run. CI
installs Node and sets ``APIARY_CI=1``, which turns that skip into a failure —
a skip on the one machine whose whole job is to run these is indistinguishable
from the suites having been silently unwired again (review plan §5a-F).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

WEB_DIR = Path(__file__).resolve().parent / "web"
# Set by .github/workflows/ci.yml. Any non-empty value counts.
CI_ENV = "APIARY_CI"
# Node prints the TAP summary at the end; on failure that tail is the useful
# part, and dumping the whole thing buries it.
_OUTPUT_TAIL_LINES = 40


def _suites() -> list[Path]:
    return sorted(WEB_DIR.glob("test_*.js"))


def _node() -> str | None:
    return shutil.which("node")


def _in_ci() -> bool:
    return bool(os.environ.get(CI_ENV, "").strip())


def _tail(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= _OUTPUT_TAIL_LINES:
        return text
    return "\n".join(["  … (earlier output trimmed)"] + lines[-_OUTPUT_TAIL_LINES:])


class JsSuitesTest(unittest.TestCase):
    def test_every_web_module_suite_is_discovered(self):
        """A typo in the glob would make this file silently test nothing."""
        names = [p.name for p in _suites()]
        self.assertTrue(names, f"no test_*.js found under {WEB_DIR}")
        # Named explicitly: a new module without a suite should be a visible
        # decision, not an omission nobody notices.
        for expected in (
            "test_bubble_monitor.js",
            "test_message_reconcile.js",
            "test_prompt_detector.js",
            "test_thinking_state.js",
        ):
            self.assertIn(expected, names)

    def test_node_suites_pass(self):
        node = _node()
        if node is None:
            if _in_ci():
                self.fail(
                    f"node is not on PATH but {CI_ENV} is set. CI must install "
                    "Node (actions/setup-node) — skipping here would mean the "
                    "frontend suites run nowhere at all."
                )
            self.skipTest("node is not on PATH — frontend suites not run")
        for suite in _suites():
            with self.subTest(suite=suite.name):
                result = subprocess.run(
                    [node, suite.name],
                    cwd=str(WEB_DIR),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
                if result.returncode != 0:
                    self.fail(
                        f"node {suite.name} failed (exit {result.returncode}):\n"
                        f"{_tail(result.stdout or '')}\n{_tail(result.stderr or '')}"
                    )


class NodeMissingPolicyTest(unittest.TestCase):
    """The skip is a local-convenience affordance, not a CI one.

    Runs the real test method under a patched ``_node`` so the outcome can be
    asserted on without needing (or not needing) a node binary.
    """

    def _run_case(self, *, ci: bool) -> unittest.TestResult:
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}._node", return_value=None),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            if ci:
                os.environ[CI_ENV] = "1"
            else:
                os.environ.pop(CI_ENV, None)
            JsSuitesTest("test_node_suites_pass").run(result)
        return result

    def test_missing_node_fails_the_run_in_ci(self):
        result = self._run_case(ci=True)
        self.assertEqual(len(result.failures), 1, result.skipped)
        self.assertFalse(result.skipped)
        self.assertIn(CI_ENV, result.failures[0][1])

    def test_missing_node_only_skips_outside_ci(self):
        result = self._run_case(ci=False)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.failures)
        self.assertFalse(result.errors)


if __name__ == "__main__":
    unittest.main()
