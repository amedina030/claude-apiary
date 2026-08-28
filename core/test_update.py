"""Tests for ``core/update.py`` — the migration chain runner.

Everything runs against a throwaway fake main-apiary with its own registry
and its own ``migrations/`` directory, so the real chain (currently a single
no-op) is never what is being asserted on.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import install as install_mod
from core import update as update_mod
from core.testing import init_git_repo as _git_init
from core.testing import make_fake_apiary as _make_fake_apiary
from core.utils import state

# A migration that records the fact it ran, so a chain's order and its
# idempotence are both observable from the target repo.
MIGRATION_TMPL = '''"""Test migration."""
from pathlib import Path

FROM_VERSION = "{from_v}"
TO_VERSION = "{to_v}"


def upgrade(repo_path: Path) -> None:
    log = Path(repo_path) / "migration-log.txt"
    with log.open("a", encoding="utf-8") as fh:
        fh.write("{from_v}->{to_v}\\n")
'''

BOOM_TMPL = '''"""Test migration that fails."""
from pathlib import Path

FROM_VERSION = "{from_v}"
TO_VERSION = "{to_v}"


def upgrade(repo_path: Path) -> None:
    raise RuntimeError("migration blew up")
'''


class UpdateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")
        self.result = install_mod.install(self.target, apiary_repo=self.apiary)

    def _set_main_version(self, version: str) -> None:
        (self.apiary / "VERSION").write_text(version + "\n", encoding="utf-8")

    def _add_migration(self, from_v: str, to_v: str, template: str = MIGRATION_TMPL) -> Path:
        name = f"v{from_v.replace('.', '_')}_to_v{to_v.replace('.', '_')}.py"
        path = self.apiary / "migrations" / name
        path.write_text(template.format(from_v=from_v, to_v=to_v), encoding="utf-8")
        return path

    def _log(self) -> list[str]:
        log = self.target / "migration-log.txt"
        if not log.is_file():
            return []
        return log.read_text(encoding="utf-8").split()

    def _pinned(self) -> str | None:
        pin = state.read_version(self.target) or {}
        return pin.get("apiary_version")

    def _registry_version(self) -> str | None:
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        return registry[str(self.result.uid)].get("version")


class VersionParsingTests(unittest.TestCase):
    def test_three_part_semver_parses(self):
        self.assertEqual(update_mod.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(update_mod.parse_version(" 0.1.0\n"), (0, 1, 0))

    def test_anything_else_is_none(self):
        for bad in ("1.2", "1.2.3.4", "0.1.0-rc1", "v0.1.0", "", "abc"):
            with self.subTest(bad=bad):
                self.assertIsNone(update_mod.parse_version(bad))


class MigrationDiscoveryTests(UpdateTestBase):
    def test_shipped_migration_loads_and_is_ordered(self):
        self._add_migration("0.1.0", "0.2.0")
        self._add_migration("0.2.0", "0.3.0")
        found = update_mod.load_migrations(self.apiary)
        # v0_0_0_to_v0_1_0.py ships in the repo and comes first.
        self.assertEqual(
            [(m.from_version, m.to_version) for m in found],
            [("0.0.0", "0.1.0"), ("0.1.0", "0.2.0"), ("0.2.0", "0.3.0")],
        )

    def test_a_migration_that_contradicts_its_filename_is_an_error(self):
        path = self.apiary / "migrations" / "v0_1_0_to_v0_2_0.py"
        path.write_text(MIGRATION_TMPL.format(from_v="0.5.0", to_v="0.6.0"), encoding="utf-8")
        with self.assertRaises(update_mod.UpdateError) as ctx:
            update_mod.load_migrations(self.apiary)
        self.assertIn("filename", str(ctx.exception))

    def test_a_migration_without_upgrade_is_an_error(self):
        path = self.apiary / "migrations" / "v0_1_0_to_v0_2_0.py"
        path.write_text(
            'FROM_VERSION = "0.1.0"\nTO_VERSION = "0.2.0"\n',
            encoding="utf-8",
        )
        with self.assertRaises(update_mod.UpdateError):
            update_mod.load_migrations(self.apiary)

    def test_chain_stops_before_overshooting_the_target(self):
        self._add_migration("0.1.0", "0.2.0")
        self._add_migration("0.2.0", "0.3.0")
        migrations = update_mod.load_migrations(self.apiary)
        chain = update_mod.plan_chain(migrations, "0.1.0", "0.2.0")
        self.assertEqual([m.to_version for m in chain], ["0.2.0"])

    def test_chain_is_empty_when_no_migration_starts_at_the_pinned_version(self):
        self._add_migration("0.4.0", "0.5.0")
        migrations = update_mod.load_migrations(self.apiary)
        self.assertEqual(update_mod.plan_chain(migrations, "0.1.0", "0.5.0"), [])


class UpdateTests(UpdateTestBase):
    def test_a_repo_already_at_the_current_version_is_untouched(self):
        report = update_mod.update(self.apiary)
        statuses = {r.name: r.status for r in report.results}
        self.assertEqual(statuses["demo"], update_mod.CURRENT)
        self.assertEqual(self._log(), [])

    def test_the_whole_chain_runs_in_order_and_re_pins(self):
        self._add_migration("0.1.0", "0.2.0")
        self._add_migration("0.2.0", "0.3.0")
        self._set_main_version("0.3.0")

        report = update_mod.update(self.apiary)
        demo = next(r for r in report.results if r.name == "demo")
        self.assertEqual(demo.status, update_mod.UPDATED)
        self.assertEqual(demo.applied, ["v0_1_0_to_v0_2_0.py", "v0_2_0_to_v0_3_0.py"])
        self.assertEqual(self._log(), ["0.1.0->0.2.0", "0.2.0->0.3.0"])
        self.assertEqual(self._pinned(), "0.3.0")
        self.assertEqual(self._registry_version(), "0.3.0")

    def test_a_gap_with_no_migration_still_moves_the_pin(self):
        self._set_main_version("0.9.0")
        report = update_mod.update(self.apiary)
        demo = next(r for r in report.results if r.name == "demo")
        self.assertEqual(demo.status, update_mod.UPDATED)
        self.assertEqual(demo.applied, [])
        self.assertEqual(self._pinned(), "0.9.0")

    def test_running_twice_applies_each_migration_once(self):
        self._add_migration("0.1.0", "0.2.0")
        self._set_main_version("0.2.0")
        update_mod.update(self.apiary)
        update_mod.update(self.apiary)
        self.assertEqual(self._log(), ["0.1.0->0.2.0"])

    def test_a_failing_migration_leaves_the_pin_at_the_last_good_version(self):
        self._add_migration("0.1.0", "0.2.0")
        self._add_migration("0.2.0", "0.3.0", template=BOOM_TMPL)
        self._add_migration("0.3.0", "0.4.0")
        self._set_main_version("0.4.0")

        report = update_mod.update(self.apiary)
        demo = next(r for r in report.results if r.name == "demo")
        self.assertEqual(demo.status, update_mod.FAILED)
        self.assertIn("migration blew up", demo.detail)
        # The first migration ran and was recorded; the third never ran.
        self.assertEqual(self._log(), ["0.1.0->0.2.0"])
        self.assertEqual(self._pinned(), "0.2.0")
        self.assertTrue(report.failed)

    def test_a_failure_in_one_repo_does_not_stop_the_others(self):
        other = _git_init(self.root / "other")
        install_mod.install(other, apiary_repo=self.apiary)
        self._add_migration("0.1.0", "0.2.0", template=BOOM_TMPL)
        self._set_main_version("0.2.0")

        report = update_mod.update(self.apiary)
        self.assertEqual(report.count(update_mod.FAILED), 2)
        self.assertEqual(len(report.results), 2)

    def test_dry_run_writes_nothing(self):
        self._add_migration("0.1.0", "0.2.0")
        self._set_main_version("0.2.0")
        report = update_mod.update(self.apiary, dry_run=True)
        demo = next(r for r in report.results if r.name == "demo")
        self.assertEqual(demo.applied, ["v0_1_0_to_v0_2_0.py"])
        self.assertEqual(self._log(), [])
        self.assertEqual(self._pinned(), "0.1.0")

    def test_target_narrows_the_run_to_one_repo(self):
        other = _git_init(self.root / "other")
        install_mod.install(other, apiary_repo=self.apiary)
        self._set_main_version("0.2.0")
        report = update_mod.update(self.apiary, target=self.target)
        self.assertEqual([r.name for r in report.results], ["demo"])

    def test_an_unregistered_target_is_an_error(self):
        stranger = self.root / "stranger"
        stranger.mkdir()
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update(self.apiary, target=stranger)

    def test_a_repo_pinned_ahead_of_main_apiary_is_skipped(self):
        state.write_version(self.target, {"apiary_version": "9.9.9", "pinned_at": "x"})
        report = update_mod.update(self.apiary)
        demo = next(r for r in report.results if r.name == "demo")
        self.assertEqual(demo.status, update_mod.SKIPPED)
        self.assertIn("ahead", demo.detail)

    def test_a_repo_whose_path_is_gone_is_skipped_not_fatal(self):
        self.target.rename(self.root / "moved-away")
        self._set_main_version("0.2.0")
        try:
            report = update_mod.update(self.apiary)
            demo = next(r for r in report.results if r.name == "demo")
            self.assertEqual(demo.status, update_mod.SKIPPED)
            self.assertIn("real_path", demo.detail)
        finally:
            (self.root / "moved-away").rename(self.target)

    def test_version_json_wins_over_a_stale_registry_entry(self):
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        registry[str(self.result.uid)]["version"] = "0.0.0"
        state._save_registry(self.apiary, registry)
        self.assertEqual(update_mod.repo_version(self.target), "0.1.0")


class UpdateCliTests(UpdateTestBase):
    def test_exit_code_is_zero_when_everything_is_current(self):
        rc = update_mod.main(["--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 0)

    def test_exit_code_is_one_when_a_migration_fails(self):
        self._add_migration("0.1.0", "0.2.0", template=BOOM_TMPL)
        self._set_main_version("0.2.0")
        rc = update_mod.main(["--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 1)

    def test_unregistered_target_exits_one_without_a_traceback(self):
        rc = update_mod.main(
            ["--apiary-repo", str(self.apiary), "--target", str(self.root / "nope")],
        )
        self.assertEqual(rc, 1)

    def test_render_names_every_outcome(self):
        self._add_migration("0.1.0", "0.2.0")
        self._set_main_version("0.2.0")
        text = update_mod.render(update_mod.update(self.apiary, dry_run=True), dry_run=True)
        self.assertIn("dry run", text)
        self.assertIn("v0_1_0_to_v0_2_0.py", text)


if __name__ == "__main__":
    unittest.main()
