"""Tests for ``core/uninstall.py`` — reverse of ``apiary install``."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import install as install_mod
from core import uninstall as uninstall_mod
from core.testing import init_git_repo as _git_init
from core.testing import make_fake_apiary as _make_fake_apiary
from core.utils import state


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")
        self.install_result = install_mod.install(self.target, apiary_repo=self.apiary)

    def test_pin_dir_is_removed(self):
        self.assertTrue(state.pin_dir(self.target).is_dir())
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertFalse(state.pin_dir(self.target).is_dir())
        self.assertTrue(result.pin_dir_removed)

    def test_apiary_commands_are_removed(self):
        cmds_dir = self.target / ".claude" / "commands"
        self.assertGreater(len(list(cmds_dir.glob("*.md"))), 0)
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertGreater(len(result.commands_removed), 0)
        # commands dir is gone or empty
        self.assertTrue(not cmds_dir.exists() or not any(cmds_dir.iterdir()))

    def test_hook_entries_removed_from_settings(self):
        settings_path = self.target / ".claude" / "settings.json"
        before = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertGreater(len(before.get("hooks", {})), 0)
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertGreater(result.hook_entries_removed, 0)
        after = json.loads(settings_path.read_text(encoding="utf-8"))
        # No apiary entries remain in any event list
        for event_entries in after.get("hooks", {}).values():
            for entry in event_entries:
                self.assertNotIn("apiary", json.dumps(entry).lower())

    def test_claude_md_zone_is_stripped(self):
        claude_md = self.target / "CLAUDE.md"
        before = claude_md.read_text(encoding="utf-8")
        self.assertIn("<!-- apiary-context-rules-start -->", before)
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertTrue(result.claude_md_zone_removed)
        after = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
        self.assertNotIn("<!-- apiary-context-rules-start -->", after)

    def test_user_owned_claude_md_content_is_preserved(self):
        claude_md = self.target / "CLAUDE.md"
        # Add a user paragraph above the zone
        text = claude_md.read_text(encoding="utf-8")
        marker = "## My Project Rules\n\nDon't break the build.\n\n"
        claude_md.write_text(marker + text, encoding="utf-8")
        uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        if claude_md.exists():
            after = claude_md.read_text(encoding="utf-8")
            self.assertIn("Don't break the build", after)

    def test_registry_entry_is_removed(self):
        uid = self.install_result.uid
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        self.assertIn(str(uid), registry)
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        registry_after = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        self.assertNotIn(str(uid), registry_after)
        self.assertTrue(result.registry_entry_removed)

    def test_state_dir_kept_by_default(self):
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertFalse(result.state_dir_removed)
        self.assertTrue(self.install_result.state_dir.is_dir())

    def test_state_dir_removed_when_remove_data_true(self):
        result = uninstall_mod.uninstall(
            self.target, apiary_repo=self.apiary, remove_data=True,
        )
        self.assertTrue(result.state_dir_removed)
        self.assertFalse(self.install_result.state_dir.is_dir())

    def test_install_then_uninstall_then_install_yields_clean_state(self):
        """Round-trip: re-installing after uninstall should work normally."""
        uninstall_mod.uninstall(self.target, apiary_repo=self.apiary, remove_data=True)
        new = install_mod.install(self.target, apiary_repo=self.apiary)
        # New uid is allocated (monotonic counter never reuses).
        self.assertNotEqual(new.uid, self.install_result.uid)
        self.assertGreater(new.uid, self.install_result.uid)
        self.assertTrue((self.target / ".claude" / "apiary" / "launch.py").is_file())


class UninstallOrderingTests(unittest.TestCase):
    """Bug 6 — the registry entry is the last thing to go.

    Deleting it first meant any later failure (a Windows ``PermissionError``
    on ``launch.py`` while a hook still has it open, a tampered CLAUDE.md)
    left a repo with pin files, hooks and commands but no registry entry —
    which is exactly the Bug 4 state the next session then compounds.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")
        self.install_result = install_mod.install(self.target, apiary_repo=self.apiary)

    def _registry(self) -> dict:
        return json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))

    def test_a_failed_file_step_leaves_the_registry_entry_intact(self):
        boom = OSError("hook process still holds launch.py")
        with mock.patch("core.uninstall.remove_hooks", side_effect=boom):
            with self.assertRaises(OSError):
                uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertIn(str(self.install_result.uid), self._registry())

    def test_a_failed_file_step_leaves_the_repo_re_uninstallable(self):
        with mock.patch("core.uninstall.remove_hooks", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        result = uninstall_mod.uninstall(self.target, apiary_repo=self.apiary)
        self.assertTrue(result.registry_entry_removed)
        self.assertNotIn(str(self.install_result.uid), self._registry())

    def test_refuses_to_uninstall_main_apiary_itself(self):
        _git_init(self.apiary)
        own = install_mod.install(self.apiary, apiary_repo=self.apiary)
        with self.assertRaises(uninstall_mod.UninstallError) as ctx:
            uninstall_mod.uninstall(self.apiary, apiary_repo=self.apiary)
        self.assertIn("main-apiary", str(ctx.exception))
        # Nothing was touched on the way to the refusal.
        self.assertTrue(state.pin_dir(self.apiary).is_dir())
        self.assertIn(str(own.uid), self._registry())


if __name__ == "__main__":
    unittest.main()
