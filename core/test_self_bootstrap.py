"""Tests for ``core/self_bootstrap.py`` — main-apiary's first-machine setup."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import self_bootstrap as sb
from core import testing
from core.utils import state


def _make_fake_apiary(root: Path) -> Path:
    """A fake main-apiary that has NOT been self-bootstrapped yet — that is
    the thing under test here. It is a git repo, because install's git-root
    check runs before anything else."""
    return testing.make_fake_apiary(root, name="fake-apiary", git=True)


class SelfBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.apiary = _make_fake_apiary(self.root)

    def test_fresh_machine_bootstrap_creates_registry_and_self_install(self):
        # Sanity: fresh fake apiary has no registry yet.
        self.assertFalse(state.registry_path(self.apiary).is_file())
        result = sb.self_bootstrap(self.apiary)
        # Registry now exists with main-apiary as uid 1
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        self.assertIn("1", registry)
        self.assertEqual(Path(registry["1"]["real_path"]), self.apiary.resolve())
        self.assertEqual(result.uid, 1)
        self.assertTrue(result.is_first_install)
        # main-apiary's own pin files exist
        pin = self.apiary / ".claude" / "apiary"
        self.assertTrue((pin / "launch.py").is_file())
        self.assertTrue((pin / "main-apiary-pointer.json").is_file())
        self.assertTrue((pin / "self-pointer.json").is_file())

    def test_main_apiary_pointer_points_at_self(self):
        sb.self_bootstrap(self.apiary)
        mp = state.read_main_apiary_pointer(self.apiary)
        self.assertEqual(Path(mp["main_apiary_path"]).resolve(), self.apiary.resolve())
        self.assertEqual(mp["main_apiary_uid"], 1)

    def test_idempotent_rerun_keeps_uid_one(self):
        first = sb.self_bootstrap(self.apiary)
        second = sb.self_bootstrap(self.apiary)
        self.assertEqual(first.uid, second.uid)
        self.assertEqual(first.uid, 1)
        self.assertFalse(second.is_first_install)

    def test_rejects_non_apiary_directory(self):
        plain = self.root / "not-apiary"
        plain.mkdir()
        with self.assertRaises(sb.SelfBootstrapError) as ctx:
            sb.self_bootstrap(plain)
        self.assertIn("does not look like a main-apiary", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
