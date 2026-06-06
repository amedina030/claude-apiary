#!/usr/bin/env python3
"""Tests that startup_prompt_hook flags GUI-hosted sessions.

When the GUI spawns claude it sets APIARY_GUI_SESSION=1 in the env, which
the hook subprocess inherits. The hook should then emit a `surface:` line
telling the session it's running inside the GUI. With the env var unset
(a raw-terminal session) the line must be absent.

cwd is pointed at the temp HOME (not a git repo) so the hook skips notes /
learnings injection and stays fast and isolated from real scribe state.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SAMPLE_SID = "abcd1234-1111-2222-3333-444444444444"
SURFACE_MARKER = "running inside the apiary GUI"


def _run_hook(home: Path, *, gui_session: bool) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # A fresh first message; cwd at home keeps it out of any git repo.
    if gui_session:
        env["APIARY_GUI_SESSION"] = "1"
    else:
        env.pop("APIARY_GUI_SESSION", None)
    payload = {"session_id": SAMPLE_SID, "message": "hi", "cwd": str(home)}
    return subprocess.run(
        [PYTHON, str(HOOKS_DIR / "startup_prompt_hook.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


class TestGuiSessionSurface(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_gui_session_emits_surface_line(self):
        result = _run_hook(self.home, gui_session=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(SURFACE_MARKER, result.stdout)

    def test_terminal_session_omits_surface_line(self):
        result = _run_hook(self.home, gui_session=False)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(SURFACE_MARKER, result.stdout)


if __name__ == "__main__":
    unittest.main()
