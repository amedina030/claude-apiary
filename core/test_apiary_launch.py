#!/usr/bin/env python3
"""Tests for core.apiary_launch.

The launcher's contract: locate the apiary repo via the pointer file or
CLAUDE_PROJECT_DIR, then run the requested script as a subprocess that
inherits the *caller's* cwd and env unchanged. Regression target is
todo T-2026-163: the previous behavior chdir'd into the apiary repo and
overrode CLAUDE_PROJECT_DIR, which made cwd-sensitive tools (scribe,
compass, researcher) resolve to apiary instead of the session's repo.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "core" / "apiary_launch.py"


class LauncherCwdEnvPassthroughTests(unittest.TestCase):
    """The launcher must not chdir into apiary or rewrite CLAUDE_PROJECT_DIR."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()
        # Caller cwd: a directory that is NOT the apiary repo.
        self.caller_cwd = self.tmp_path / "caller_repo"
        self.caller_cwd.mkdir()
        # Fake apiary repo with a probe script.
        self.fake_apiary = self.tmp_path / "fake_apiary"
        self.fake_apiary.mkdir()
        self.probe = self.fake_apiary / "probe.py"
        self.probe.write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.stdout.write('CWD=' + str(Path.cwd().resolve()) + '\\n')\n"
            "sys.stdout.write('CPD=' + os.environ.get('CLAUDE_PROJECT_DIR', '') + '\\n')\n",
            encoding="utf-8",
        )
        # Pointer file that tells the launcher where apiary lives.
        self.pointer_dir = self.tmp_path / "home" / ".claude"
        self.pointer_dir.mkdir(parents=True)
        self.pointer = self.pointer_dir / "apiary.json"
        self.pointer.write_text(
            json.dumps({"version": 1, "repo_path": str(self.fake_apiary)}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, env_overrides=None):
        env = {**os.environ}
        # Redirect HOME / USERPROFILE so the launcher reads OUR pointer file.
        env["HOME"] = str(self.pointer_dir.parent)
        env["USERPROFILE"] = str(self.pointer_dir.parent)
        if env_overrides:
            env.update(env_overrides)
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "probe.py"],
            cwd=str(self.caller_cwd),
            env=env,
            capture_output=True,
            text=True,
        )
        return result

    def test_subprocess_inherits_caller_cwd(self):
        """The probe must report the caller's cwd, not the apiary repo."""
        result = self._run()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        cwd_line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("CWD=")),
            None,
        )
        self.assertIsNotNone(cwd_line, msg=f"no CWD= line in stdout: {result.stdout!r}")
        reported_cwd = Path(cwd_line[len("CWD="):]).resolve()
        self.assertEqual(reported_cwd, self.caller_cwd.resolve())
        self.assertNotEqual(reported_cwd, self.fake_apiary.resolve())

    def test_subprocess_does_not_override_claude_project_dir(self):
        """An existing CLAUDE_PROJECT_DIR must reach the subprocess unchanged."""
        session_dir = str(self.caller_cwd.resolve())
        result = self._run(env_overrides={"CLAUDE_PROJECT_DIR": session_dir})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        cpd_line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("CPD=")),
            None,
        )
        self.assertIsNotNone(cpd_line, msg=f"no CPD= line in stdout: {result.stdout!r}")
        self.assertEqual(cpd_line[len("CPD="):], session_dir)


if __name__ == "__main__":
    unittest.main()
