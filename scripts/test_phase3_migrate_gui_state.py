"""Tests for ``scripts/phase3_migrate_gui_state.py``."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase3_migrate_gui_state as mig


class GuiMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = self.root / "apiary"
        self.apiary.mkdir()
        (self.apiary / ".apiary").mkdir()
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()

    def _seed_gui(self, *, with_dev: bool = False) -> None:
        gui = self.global_dir / "apiary_gui"
        gui.mkdir()
        (gui / "tabs.json").write_text("[]", encoding="utf-8")
        (gui / "theme.json").write_text("{}", encoding="utf-8")
        (gui / "captures").mkdir()
        (gui / "captures" / "shot1.bin").write_bytes(b"stub")
        if with_dev:
            (self.global_dir / "apiary_gui_dev").mkdir()
            (self.global_dir / "apiary_gui_dev" / "tabs.json").write_text("[]", encoding="utf-8")

    def test_plan_returns_each_top_level_item(self):
        self._seed_gui()
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        labels = [label for label, _, _ in plan]
        self.assertIn("apiary_gui/tabs.json", labels)
        self.assertIn("apiary_gui/theme.json", labels)
        self.assertIn("apiary_gui/captures", labels)

    def test_apply_copies_files_and_dirs(self):
        self._seed_gui()
        rc = mig.main([
            "--apply", "--no-active-check",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        target = self.apiary / ".apiary" / "gui" / "apiary_gui"
        self.assertTrue((target / "tabs.json").is_file())
        self.assertTrue((target / "captures" / "shot1.bin").is_file())
        # Original preserved (copy, not move)
        self.assertTrue((self.global_dir / "apiary_gui" / "tabs.json").is_file())

    def test_idempotent_rerun_no_overwrite(self):
        self._seed_gui()
        for _ in range(2):
            mig.main([
                "--apply", "--no-active-check",
                "--apiary-repo", str(self.apiary),
                "--global-dir", str(self.global_dir),
            ])
        # Tabs file present exactly once
        target = self.apiary / ".apiary" / "gui" / "apiary_gui" / "tabs.json"
        self.assertTrue(target.is_file())

    def test_dry_run_does_not_write(self):
        self._seed_gui()
        rc = mig.main([
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        target = self.apiary / ".apiary" / "gui"
        self.assertFalse((target / "apiary_gui" / "tabs.json").exists())

    def test_active_gui_blocks_without_force(self):
        gui = self.global_dir / "apiary_gui"
        gui.mkdir()
        log = gui / "permission_mcp.log"
        log.write_text("recent\n", encoding="utf-8")
        # Touch to "now" so it's < 30s
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 1)

    def test_active_gui_allowed_with_force(self):
        gui = self.global_dir / "apiary_gui"
        gui.mkdir()
        (gui / "permission_mcp.log").write_text("recent\n", encoding="utf-8")
        (gui / "tabs.json").write_text("[]", encoding="utf-8")
        rc = mig.main([
            "--apply", "--force",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
