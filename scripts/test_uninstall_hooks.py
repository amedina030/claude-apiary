#!/usr/bin/env python3
"""Tests for scripts/uninstall_hooks.py and core.hooks_lib.remove_hooks (#261)."""
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import remove_hooks  # noqa: E402
from scripts import uninstall_hooks  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

APIARY_ABSOLUTE_ENTRY = {
    "matcher": "Bash",
    "hooks": [{
        "type": "command",
        "command": "/c/Users/foo/claude-apiary/budgeter/hooks/pre_tool_use.py",
    }],
}

APIARY_PORTABLE_ENTRY = {
    "matcher": "",
    "hooks": [{
        "type": "command",
        "command": "python $CLAUDE_PROJECT_DIR/core/hooks/inject_session.py",
    }],
}

APIARY_SCRIBE_ENTRY = {
    "hooks": [{
        "type": "command",
        "command": "python $CLAUDE_PROJECT_DIR/scribe/commands/save_transcript.py",
    }],
}

THIRD_PARTY_ENTRY = {
    "matcher": "Bash",
    "hooks": [{
        "type": "command",
        "command": "python /opt/other-tool/hooks/pre.py",
    }],
}


def _make_settings(hooks: dict) -> dict:
    return {"hooks": hooks, "someOtherSetting": "preserved"}


# ---------------------------------------------------------------------------
# remove_hooks tests
# ---------------------------------------------------------------------------

class RemoveHooksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings_path = self.tmp_path / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, settings: dict):
        self.settings_path.write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

    def _load(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_missing_file_is_noop(self):
        report = remove_hooks(self.settings_path)
        self.assertFalse(report["existed"])
        self.assertEqual(report["removed"], [])
        self.assertFalse(self.settings_path.exists())

    def test_removes_absolute_path_apiary_entry(self):
        self._write(_make_settings({"PreToolUse": [APIARY_ABSOLUTE_ENTRY]}))
        report = remove_hooks(self.settings_path)
        self.assertEqual(len(report["removed"]), 1)
        self.assertNotIn("hooks", self._load())

    def test_removes_portable_claude_project_dir_entry(self):
        self._write(_make_settings({"PreToolUse": [APIARY_PORTABLE_ENTRY]}))
        report = remove_hooks(self.settings_path)
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(report["removed"][0]["event"], "PreToolUse")
        self.assertNotIn("hooks", self._load())

    def test_preserves_third_party_entries(self):
        self._write(_make_settings({
            "PreToolUse": [APIARY_ABSOLUTE_ENTRY, THIRD_PARTY_ENTRY],
            "PostToolUse": [THIRD_PARTY_ENTRY],
        }))
        report = remove_hooks(self.settings_path)
        self.assertEqual(len(report["removed"]), 1)
        loaded = self._load()
        self.assertEqual(loaded["hooks"]["PreToolUse"], [THIRD_PARTY_ENTRY])
        self.assertEqual(loaded["hooks"]["PostToolUse"], [THIRD_PARTY_ENTRY])
        self.assertEqual(loaded["someOtherSetting"], "preserved")

    def test_mixed_events_with_prune(self):
        """Event lists containing only apiary entries must be pruned."""
        self._write(_make_settings({
            "PreToolUse": [APIARY_ABSOLUTE_ENTRY, THIRD_PARTY_ENTRY],
            "Stop": [APIARY_SCRIBE_ENTRY],  # only ours — should disappear
        }))
        remove_hooks(self.settings_path)
        loaded = self._load()
        self.assertIn("PreToolUse", loaded["hooks"])
        self.assertNotIn("Stop", loaded["hooks"])

    def test_top_level_hooks_key_removed_when_all_empty(self):
        self._write(_make_settings({
            "PreToolUse": [APIARY_ABSOLUTE_ENTRY],
            "Stop": [APIARY_SCRIBE_ENTRY],
        }))
        remove_hooks(self.settings_path)
        loaded = self._load()
        self.assertNotIn("hooks", loaded)
        self.assertEqual(loaded["someOtherSetting"], "preserved")

    def test_dry_run_reports_but_does_not_write(self):
        self._write(_make_settings({"PreToolUse": [APIARY_ABSOLUTE_ENTRY]}))
        original = self.settings_path.read_text(encoding="utf-8")
        report = remove_hooks(self.settings_path, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(
            self.settings_path.read_text(encoding="utf-8"), original
        )

    def test_non_list_event_value_preserved(self):
        """Settings files with unexpected shapes shouldn't crash."""
        self._write({"hooks": {"PreToolUse": "unexpected-string"}})
        report = remove_hooks(self.settings_path)
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            self._load()["hooks"]["PreToolUse"], "unexpected-string"
        )

    def test_non_dict_hooks_value_preserved(self):
        self._write({"hooks": ["not-a-dict"]})
        report = remove_hooks(self.settings_path)
        self.assertEqual(report["removed"], [])
        # File is untouched — no 'hooks' rewrite
        self.assertEqual(self._load()["hooks"], ["not-a-dict"])

    def test_remaining_counts_match_post_state(self):
        self._write(_make_settings({
            "PreToolUse": [
                APIARY_ABSOLUTE_ENTRY,
                THIRD_PARTY_ENTRY,
                THIRD_PARTY_ENTRY,
            ],
            "PostToolUse": [APIARY_PORTABLE_ENTRY],
        }))
        report = remove_hooks(self.settings_path)
        self.assertEqual(report["remaining_counts"].get("PreToolUse"), 2)
        self.assertNotIn("PostToolUse", report["remaining_counts"])


# ---------------------------------------------------------------------------
# CLI / main() tests
# ---------------------------------------------------------------------------

class UninstallCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings_path = self.tmp_path / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, settings: dict):
        self.settings_path.write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

    def _run(self, *args) -> tuple[int, str]:
        """Invoke uninstall_hooks.main() with a synthetic argv and captured stdout."""
        argv = ["uninstall_hooks.py", *args]
        buf = StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stdout", buf):
            code = uninstall_hooks.main()
        return code, buf.getvalue()

    def test_list_on_missing_file(self):
        code, out = self._run("--list", "--settings", str(self.settings_path))
        self.assertEqual(code, 0)
        self.assertIn("does not exist", out)

    def test_list_reports_owned_entries(self):
        self._write(_make_settings({
            "PreToolUse": [APIARY_ABSOLUTE_ENTRY, THIRD_PARTY_ENTRY],
            "Stop": [APIARY_SCRIBE_ENTRY],
        }))
        code, out = self._run("--list", "--settings", str(self.settings_path))
        self.assertEqual(code, 0)
        self.assertIn("apiary-owned entries: 2", out)
        self.assertIn("PreToolUse", out)
        self.assertIn("Stop", out)
        self.assertIn("budgeter/hooks/pre_tool_use.py", out)
        # Third-party entries must not be listed as owned
        self.assertNotIn("other-tool", out)

    def test_list_does_not_modify_file(self):
        self._write(_make_settings({"PreToolUse": [APIARY_ABSOLUTE_ENTRY]}))
        original = self.settings_path.read_text(encoding="utf-8")
        self._run("--list", "--settings", str(self.settings_path))
        self.assertEqual(
            self.settings_path.read_text(encoding="utf-8"), original
        )

    def test_dry_run_does_not_modify_file(self):
        self._write(_make_settings({"PreToolUse": [APIARY_ABSOLUTE_ENTRY]}))
        original = self.settings_path.read_text(encoding="utf-8")
        code, out = self._run(
            "--dry-run", "--settings", str(self.settings_path)
        )
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(
            self.settings_path.read_text(encoding="utf-8"), original
        )

    def test_uninstall_modifies_file(self):
        self._write(_make_settings({
            "PreToolUse": [APIARY_ABSOLUTE_ENTRY, THIRD_PARTY_ENTRY],
        }))
        code, out = self._run(
            "--uninstall", "--settings", str(self.settings_path)
        )
        self.assertEqual(code, 0)
        self.assertIn("removed: 1", out)
        loaded = json.loads(
            self.settings_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            loaded["hooks"]["PreToolUse"], [THIRD_PARTY_ENTRY]
        )

    def test_uninstall_noop_on_clean_file(self):
        self._write({"hooks": {}, "otherKey": "value"})
        code, out = self._run(
            "--uninstall", "--settings", str(self.settings_path)
        )
        self.assertEqual(code, 0)
        self.assertIn("no apiary-owned hook entries", out)

    def test_mutually_exclusive_modes(self):
        """argparse enforces exactly one of --list/--dry-run/--uninstall."""
        with self.assertRaises(SystemExit):
            self._run("--settings", str(self.settings_path))


if __name__ == "__main__":
    unittest.main()
