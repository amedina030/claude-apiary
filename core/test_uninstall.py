"""Tests for ``core/uninstall.py`` — reverse of ``apiary install``."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import install as install_mod
from core import uninstall as uninstall_mod
from core.utils import state

# Mirror the test_install fixture set so a single tmpdir can host both.
_APIARY_ITEMS = (
    "setup.py", "VERSION", "core", "profiles", "context-rules", "migrations",
    "budgeter", "scribe", "docs", "refiner", "harden",
    "compass", "researcher", "runner", "incubator",
)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )


def _make_fake_apiary(root: Path) -> Path:
    fake = root / "apiary_copy"
    fake.mkdir()
    for item in _APIARY_ITEMS:
        src = REPO_ROOT / item
        if src.is_dir():
            shutil.copytree(src, fake / item, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, fake / item)
    (fake / ".repos").mkdir()
    (fake / ".apiary" / "forwarding").mkdir(parents=True)
    return fake


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = self.root / "demo"
        self.target.mkdir()
        _git_init(self.target)
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


if __name__ == "__main__":
    unittest.main()
