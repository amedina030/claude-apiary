#!/usr/bin/env python3
"""Tests for setup.py helpers — specifically the run_check() drift detection
that was reporting false negatives before #227.

Two helpers under test:
  - _is_apiary_entry(entry): recognizes both absolute-path and portable
    $CLAUDE_PROJECT_DIR-form hook entries as ours.
  - _expand_hook_script_path(s): resolves $CLAUDE_PROJECT_DIR, env vars,
    ~, and bash-style /c/Users/... paths to a real filesystem Path.
"""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import setup as apiary_setup  # noqa: E402


class TestIsApiaryEntry(unittest.TestCase):
    def test_absolute_path_with_marker(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "/c/Users/foo/claude-apiary/budgeter/hooks/pre_tool_use.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_portable_claude_project_dir_form(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python $CLAUDE_PROJECT_DIR/budgeter/hooks/pre_tool_use.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_core_hooks_subpath(self):
        entry = {
            "matcher": "",
            "hooks": [{"type": "command", "command": "python $CLAUDE_PROJECT_DIR/core/hooks/inject_session.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_third_party_hook_not_recognized(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python /opt/some-other-tool/hooks/foo.py"}],
        }
        self.assertFalse(apiary_setup._is_apiary_entry(entry))

    def test_empty_entry(self):
        self.assertFalse(apiary_setup._is_apiary_entry({}))


class TestExpandHookScriptPath(unittest.TestCase):
    def test_claude_project_dir_resolves_to_repo_root(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/setup.py")
        self.assertTrue(p.exists())
        self.assertEqual(p.resolve(), (REPO_ROOT / "setup.py").resolve())

    def test_known_apiary_hook_resolves(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/core/hooks/inject_session.py")
        self.assertTrue(p.exists())

    def test_nonexistent_path_returns_path(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/no_such_file.py")
        self.assertFalse(p.exists())  # but no exception

    def test_env_var_expansion(self):
        os.environ["_APIARY_TEST_VAR"] = str(REPO_ROOT)
        try:
            p = apiary_setup._expand_hook_script_path("$_APIARY_TEST_VAR/setup.py")
            self.assertTrue(p.exists())
        finally:
            del os.environ["_APIARY_TEST_VAR"]

    def test_bash_style_drive_letter_conversion(self):
        # Build a /<drive>/... form for the repo root and verify it resolves.
        win_path = str(REPO_ROOT / "setup.py")
        # Only run on Windows-style paths (drive letter present).
        if len(win_path) >= 2 and win_path[1] == ":":
            drive = win_path[0].lower()
            rest = win_path[2:].replace("\\", "/")
            bash_path = f"/{drive}{rest}"
            p = apiary_setup._expand_hook_script_path(bash_path)
            self.assertTrue(p.exists(), f"failed for bash_path={bash_path!r} → {p!r}")


if __name__ == "__main__":
    unittest.main()
