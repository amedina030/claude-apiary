"""Run the frontend's Node test suites from pytest.

`gui/web/*.js` holds the pure, browser-free modules extracted out of app.js
(prompt detection, bubble-anomaly classification, message reconciliation, the
thinking-bubble state machine). Each has a `node:test` suite next to it, and
until this file existed those suites were wired into nothing — no pytest
bridge, no script, no CI — so they only ran when somebody remembered to type
`node gui/web/test_*.js`. This makes `pytest gui` run them too.

Node is not a hard dependency of the repo: when it isn't on PATH the suites
SKIP rather than fail, so a Python-only checkout still gets a green run. CI
installs Node, so there the suites really execute (review plan §5a-F).
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
# Node prints the TAP summary at the end; on failure that tail is the useful
# part, and dumping the whole thing buries it.
_OUTPUT_TAIL_LINES = 40


def _suites() -> list[Path]:
    return sorted(WEB_DIR.glob("test_*.js"))


def _node() -> str | None:
    return shutil.which("node")


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


if __name__ == "__main__":
    unittest.main()
