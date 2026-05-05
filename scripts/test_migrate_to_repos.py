"""Tests for scripts/migrate_to_repos.py."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.utils import state
from scripts import migrate_to_repos as mig


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = self.root / "apiary"
        self.apiary.mkdir()
        self._resolve_patch = mock.patch.object(
            state, "resolve_apiary_repo", return_value=self.apiary,
        )
        self._resolve_patch.start()
        self.addCleanup(self._resolve_patch.stop)

    def _make_target_with_state(self, name: str = "target") -> Path:
        target = self.root / name
        target.mkdir()
        _git_init(target)
        # Seed legacy in-repo state under .apiary/
        legacy = target / ".apiary"
        legacy.mkdir()
        (legacy / "scribe").mkdir()
        (legacy / "scribe" / "marker.txt").write_text("scribe-state", encoding="utf-8")
        (legacy / "runner").mkdir()
        (legacy / "runner" / "intake.json").write_text("{}", encoding="utf-8")
        return target

    # --- Happy path ---------------------------------------------------

    def test_migrates_state_and_writes_pointer(self):
        target = self._make_target_with_state("ue-llm-toolkit")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mig.migrate(target)
        self.assertEqual(rc, 0)

        # State copied into <apiary>/.repos/<name>-<id>/
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), 1)
        only_id, entry = next(iter(registry.items()))
        state_dir = self.apiary / ".repos" / f"{entry['name']}-{only_id}"
        self.assertTrue((state_dir / "scribe" / "marker.txt").is_file())
        self.assertEqual(
            (state_dir / "scribe" / "marker.txt").read_text(encoding="utf-8"),
            "scribe-state",
        )
        self.assertTrue((state_dir / "runner" / "intake.json").is_file())

        # Original snapshot at .apiary.pre-migration/
        self.assertTrue((target / ".apiary.pre-migration").is_dir())
        self.assertTrue((target / ".apiary.pre-migration" / "scribe" / "marker.txt").is_file())

        # Fresh pointer file in .apiary/
        pointer = target / ".apiary" / "pointer"
        self.assertTrue(pointer.is_file())
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        self.assertEqual(payload["target_id"], state_dir.name)

    def test_dry_run_changes_nothing(self):
        target = self._make_target_with_state("dryrun")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mig.migrate(target, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertFalse((target / ".apiary.pre-migration").exists())
        # Original state still in place
        self.assertTrue((target / ".apiary" / "scribe" / "marker.txt").is_file())

    # --- Error cases --------------------------------------------------

    def test_errors_on_missing_target(self):
        ghost = self.root / "does_not_exist"
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = mig.migrate(ghost)
        self.assertEqual(rc, 1)
        self.assertIn("not a directory", buf.getvalue())

    def test_errors_when_no_apiary_dir(self):
        target = self.root / "no_state"
        target.mkdir()
        _git_init(target)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = mig.migrate(target)
        self.assertEqual(rc, 1)
        self.assertIn("nothing to migrate", buf.getvalue())

    def test_errors_when_already_migrated(self):
        target = self._make_target_with_state("twice")
        # First run completes
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mig.migrate(target), 0)
        # Second run sees pre-migration dir
        err = io.StringIO()
        with redirect_stderr(err):
            rc = mig.migrate(target)
        self.assertEqual(rc, 1)
        self.assertIn("already migrated", err.getvalue())

    def test_errors_when_only_pointer_in_apiary(self):
        target = self.root / "lazy_only"
        target.mkdir()
        _git_init(target)
        (target / ".apiary").mkdir()
        (target / ".apiary" / "pointer").write_text("{}", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = mig.migrate(target)
        self.assertEqual(rc, 1)
        self.assertIn("nothing to migrate", buf.getvalue())

    def test_errors_when_state_dir_already_has_content(self):
        target = self._make_target_with_state("collide")
        # Pre-populate the destination .repos/<name>-<id>/ — simulates
        # a half-completed prior migration.
        state.repos_dir(self.apiary).mkdir(parents=True, exist_ok=True)
        # First, run resolver to create the entry & dir
        state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        only_id = next(iter(registry.keys()))
        state_dir = self.apiary / ".repos" / f"{registry[only_id]['name']}-{only_id}"
        # Drop a file in there
        (state_dir / "scribe").mkdir()
        (state_dir / "scribe" / "stale.txt").write_text("x", encoding="utf-8")

        err = io.StringIO()
        with redirect_stderr(err):
            rc = mig.migrate(target)
        self.assertEqual(rc, 1)
        self.assertIn("not empty", err.getvalue())


class CLIArgsTests(unittest.TestCase):
    def test_target_required(self):
        # argparse exits 2 on missing required arg
        with self.assertRaises(SystemExit) as ctx:
            mig.main([])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
