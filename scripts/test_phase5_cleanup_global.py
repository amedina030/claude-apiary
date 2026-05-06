"""Tests for ``scripts/phase5_cleanup_global.py``."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase5_cleanup_global as cleanup


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.g = Path(self._tmp.name)

    def test_empty_global_dir_yields_empty_plan(self):
        self.assertEqual(cleanup.plan(self.g), [])

    def test_top_level_files_detected(self):
        for name in ("apiary.json", "apiary_launch.py", ".install-manifest.json"):
            (self.g / name).write_text("x", encoding="utf-8")
        labels = [label for label, _ in cleanup.plan(self.g)]
        self.assertEqual(labels.count("file"), 3)

    def test_flag_files_detected(self):
        (self.g / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        (self.g / "auto-startup-enabled").write_text("on", encoding="utf-8")
        labels = [label for label, _ in cleanup.plan(self.g)]
        self.assertEqual(labels.count("flag"), 2)

    def test_directories_detected(self):
        (self.g / "apiary_gui").mkdir()
        (self.g / "transcripts").mkdir()
        labels = [label for label, _ in cleanup.plan(self.g)]
        self.assertEqual(labels.count("dir"), 2)

    def test_apiary_slash_commands_detected_user_commands_left_alone(self):
        cmds = self.g / "commands"
        cmds.mkdir()
        (cmds / "wrapup.md").write_text("x", encoding="utf-8")
        (cmds / "note.md").write_text("x", encoding="utf-8")
        # User-owned slash command — must NOT be flagged
        (cmds / "my-custom.md").write_text("x", encoding="utf-8")
        plan = cleanup.plan(self.g)
        names = [p.name for _, p in plan]
        self.assertIn("wrapup.md", names)
        self.assertIn("note.md", names)
        self.assertNotIn("my-custom.md", names)

    def test_session_identity_files_detected_via_glob(self):
        (self.g / ".session-identity-abcd1234.json").write_text("{}", encoding="utf-8")
        (self.g / ".session-identity-deadbeef.json").write_text("{}", encoding="utf-8")
        labels = [label for label, _ in cleanup.plan(self.g)]
        self.assertEqual(labels.count("glob"), 2)


class RemoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.g = Path(self._tmp.name)

    def test_remove_deletes_files_and_dirs(self):
        (self.g / "apiary.json").write_text("x", encoding="utf-8")
        gui = self.g / "apiary_gui"
        gui.mkdir()
        (gui / "tabs.json").write_text("[]", encoding="utf-8")
        plan = cleanup.plan(self.g)
        self.assertEqual(cleanup.remove(plan), 2)
        self.assertFalse((self.g / "apiary.json").exists())
        self.assertFalse(gui.exists())

    def test_idempotent_rerun(self):
        (self.g / "apiary.json").write_text("x", encoding="utf-8")
        cleanup.remove(cleanup.plan(self.g))
        self.assertEqual(cleanup.plan(self.g), [])
        self.assertEqual(cleanup.remove(cleanup.plan(self.g)), 0)


class ZoneCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.g = Path(self._tmp.name)

    def test_apply_blocked_when_zone_still_present(self):
        (self.g / "CLAUDE.md").write_text(
            "<!-- apiary-context-rules-start -->\n\n"
            "<!-- apiary-context-rules-end -->\n",
            encoding="utf-8",
        )
        (self.g / "apiary.json").write_text("x", encoding="utf-8")
        rc = cleanup.main(["--apply", "--global-dir", str(self.g)])
        self.assertEqual(rc, 1)
        self.assertTrue((self.g / "apiary.json").exists())  # not removed

    def test_skip_zone_check_proceeds(self):
        (self.g / "CLAUDE.md").write_text(
            "<!-- apiary-context-rules-start -->\n"
            "<!-- apiary-context-rules-end -->\n",
            encoding="utf-8",
        )
        (self.g / "apiary.json").write_text("x", encoding="utf-8")
        rc = cleanup.main([
            "--apply", "--skip-zone-check", "--global-dir", str(self.g),
        ])
        self.assertEqual(rc, 0)
        self.assertFalse((self.g / "apiary.json").exists())


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.g = Path(self._tmp.name)

    def test_dry_run_does_not_delete(self):
        (self.g / "apiary.json").write_text("x", encoding="utf-8")
        rc = cleanup.main(["--global-dir", str(self.g)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.g / "apiary.json").exists())

    def test_empty_global_returns_zero_with_no_action(self):
        rc = cleanup.main(["--apply", "--global-dir", str(self.g)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
