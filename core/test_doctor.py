"""Tests for ``core/doctor.py`` — read-only consistency checks."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import doctor
from core.utils import state


def _make_apiary(root: Path, version: str = "0.1.0") -> Path:
    """Build a minimal main-apiary checkout under *root* and return its path."""
    apiary = root / "apiary"
    apiary.mkdir()
    (apiary / "VERSION").write_text(version + "\n", encoding="utf-8")
    (apiary / ".repos").mkdir()
    return apiary


def _write_registry(apiary: Path, data: dict) -> None:
    p = state.registry_path(apiary)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


class CheckRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_clean_registry_returns_no_findings(self):
        repo_path = self.root / "myrepo"
        repo_path.mkdir()
        _write_registry(self.apiary, {
            "1": {"name": "myrepo", "real_path": str(repo_path), "uid": 1, "version": "0.1.0"},
        })
        notes, issues = doctor.check_registry(self.apiary)
        self.assertEqual(issues, [])
        self.assertEqual(notes, [])

    def test_missing_uid_field_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "version": "0.1.0"},
        })
        _, issues = doctor.check_registry(self.apiary)
        self.assertTrue(any("missing `uid`" in i for i in issues))

    def test_missing_version_field_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "uid": 1},
        })
        _, issues = doctor.check_registry(self.apiary)
        self.assertTrue(any("missing `version`" in i for i in issues))

    def test_uid_disagreeing_with_key_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "uid": 99, "version": "0.1.0"},
        })
        _, issues = doctor.check_registry(self.apiary)
        self.assertTrue(any("disagrees with key" in i for i in issues))

    def test_nonexistent_real_path_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "ghost", "real_path": str(self.root / "nope"), "uid": 1, "version": "0.1.0"},
        })
        _, issues = doctor.check_registry(self.apiary)
        self.assertTrue(any("does not exist" in i for i in issues))


class CheckPointersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_missing_self_pointer_is_a_note_not_an_issue(self):
        notes, issues = doctor.check_pointers(self.apiary)
        self.assertEqual(issues, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("self-pointer not yet written", notes[0])

    def test_drifted_self_pointer_is_an_issue(self):
        state.write_self_pointer(self.apiary, {
            "uid": 1, "name": "claude-apiary", "real_path": "/wrong/path",
        })
        _, issues = doctor.check_pointers(self.apiary)
        self.assertTrue(any("self-pointer drift" in i for i in issues))

    def test_aligned_self_pointer_returns_clean(self):
        state.write_self_pointer(self.apiary, {
            "uid": 1, "name": "claude-apiary", "real_path": str(self.apiary.resolve()),
        })
        notes, issues = doctor.check_pointers(self.apiary)
        self.assertEqual(notes, [])
        self.assertEqual(issues, [])


class CheckVersionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root, version="0.2.0")

    def test_pinned_version_matching_main_returns_clean(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "uid": 1, "version": "0.2.0"},
        })
        _, issues = doctor.check_versions(self.apiary)
        self.assertEqual(issues, [])

    def test_pinned_version_diverging_from_main_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "uid": 1, "version": "0.1.0"},
        })
        _, issues = doctor.check_versions(self.apiary)
        # The remediation is `apiary update`, not a re-install: re-installing
        # rewrites files but never runs a migration (review §5a-I).
        self.assertTrue(any("apiary update --target" in i for i in issues))

    def test_missing_version_field_is_an_issue(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root), "uid": 1},
        })
        _, issues = doctor.check_versions(self.apiary)
        self.assertTrue(any("no `version` field" in i for i in issues))


class CheckOrphansAndDuplicatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_orphan_state_dir_with_unknown_uid(self):
        # registry has uid 1; .repos/ has a folder with uid 99
        _write_registry(self.apiary, {
            "1": {"name": "real", "real_path": str(self.root), "uid": 1, "version": "0.1.0"},
        })
        (self.apiary / ".repos" / "ghost-99").mkdir()
        _, issues = doctor.check_orphans(self.apiary)
        self.assertTrue(any("ghost-99" in i for i in issues))

    def test_unparseable_slug_is_an_issue(self):
        _write_registry(self.apiary, {})
        (self.apiary / ".repos" / "no_uid_suffix").mkdir()
        _, issues = doctor.check_orphans(self.apiary)
        self.assertTrue(any("unparseable" in i for i in issues))

    def test_duplicate_real_path_is_an_issue(self):
        same = str(self.root / "shared")
        _write_registry(self.apiary, {
            "1": {"name": "a", "real_path": same, "uid": 1, "version": "0.1.0"},
            "2": {"name": "b", "real_path": same, "uid": 2, "version": "0.1.0"},
        })
        _, issues = doctor.check_duplicates(self.apiary)
        self.assertTrue(any("duplicate real_path" in i for i in issues))


class CheckUnreachableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_real_path_missing_on_disk_is_unreachable(self):
        _write_registry(self.apiary, {
            "1": {"name": "gone", "real_path": str(self.root / "missing"),
                  "uid": 1, "version": "0.1.0"},
        })
        _, issues = doctor.check_unreachable(self.apiary)
        self.assertTrue(any("unreachable" in i for i in issues))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_main_returns_zero_on_clean_registry(self):
        repo = self.root / "x"
        repo.mkdir()
        # main-apiary holds uid 1 — `check_pins` reports it when anything else
        # does, because the drift handler acts on that convention.
        _write_registry(self.apiary, {
            "1": {"name": "apiary", "real_path": str(self.apiary), "uid": 1,
                  "version": "0.1.0"},
            "2": {"name": "x", "real_path": str(repo), "uid": 2, "version": "0.1.0"},
        })
        rc = doctor.main(["--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 0)

    def test_main_returns_one_when_registry_has_issues(self):
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(self.root / "missing"),
                  "uid": 1, "version": "0.1.0"},
        })
        rc = doctor.main(["registry", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 1)


class FixActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_fix_without_subcommand_errors(self):
        rc = doctor.main(["--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 2)

    def test_fix_unsupported_subcommand_errors(self):
        rc = doctor.main(["registry", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 2)

    def test_fix_pointers_runs_cascade(self):
        # No bootstrapped repos beyond main-apiary itself → nothing to update,
        # but the action should still run cleanly and report 0 updates.
        _write_registry(self.apiary, {
            "1": {"name": "main", "real_path": str(self.apiary),
                  "uid": 1, "version": "0.1.0"},
        })
        rc = doctor.main(["pointers", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 0)


class CheckStaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def _add_source_command(self, filename: str, content: str) -> Path:
        """Write a source command file under a recognized tool dir."""
        cmd_dir = self.apiary / "scribe" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        p = cmd_dir / filename
        p.write_text(content, encoding="utf-8")
        return p

    def _register(self, uid: int, name: str, real_path: Path) -> None:
        _write_registry(self.apiary, {
            str(uid): {"uid": uid, "name": name,
                       "real_path": str(real_path), "version": "0.1.0"},
        })

    def _write_bootstrap_state(self, slug: str, command_hashes: dict) -> None:
        d = state.repos_dir(self.apiary) / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "bootstrap_state.json").write_text(
            json.dumps({"commands_dir_hashes": command_hashes}), encoding="utf-8")

    def test_matching_hashes_returns_clean(self):
        from core.install import _hash_file
        src = self._add_source_command("notes.md", "current\n")
        repo = self.root / "repo"
        repo.mkdir()
        self._register(2, "repo", repo)
        self._write_bootstrap_state("repo-2", {"notes.md": _hash_file(src)})
        notes, issues = doctor.check_stale(self.apiary)
        self.assertEqual(notes, [])
        self.assertEqual(issues, [])

    def test_diverging_hash_is_an_issue(self):
        self._add_source_command("notes.md", "edited-since-install\n")
        repo = self.root / "repo"
        repo.mkdir()
        self._register(2, "repo", repo)
        self._write_bootstrap_state("repo-2", {"notes.md": "0" * 64})  # stale hash
        notes, issues = doctor.check_stale(self.apiary)
        self.assertEqual(len(issues), 1)
        self.assertIn("notes.md", issues[0])
        self.assertIn("repo", issues[0])

    def test_missing_bootstrap_state_is_a_note_not_an_issue(self):
        self._add_source_command("notes.md", "current\n")
        repo = self.root / "repo"
        repo.mkdir()
        self._register(2, "repo", repo)
        # No bootstrap_state.json written.
        notes, issues = doctor.check_stale(self.apiary)
        self.assertEqual(issues, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("bootstrap_state", notes[0])

    def test_unreachable_repo_is_skipped(self):
        self._add_source_command("notes.md", "current\n")
        self._register(2, "repo", self.root / "does-not-exist")
        notes, issues = doctor.check_stale(self.apiary)
        self.assertEqual(issues, [])
        self.assertEqual(notes, [])


class CheckPinsTests(unittest.TestCase):
    """Bug 4/5 — nothing used to compare a repo's pin files to the registry.

    A repo whose ``self-pointer.uid`` disagrees with its registry key sends the
    launcher looking for ``<name>-<pinned-uid>``, which does not exist, so every
    tool silently falls back to an in-repo path. It is invisible until someone
    notices their notes went missing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        # The healthy shape: main-apiary is uid 1, the bootstrapped repo is
        # uid 2, and both carry pins that agree with the registry.
        self._register()
        self._write_pins(self.apiary, uid=1, name="apiary")
        self._write_pins(self.repo, uid=2, name="repo")

    def _register(self, repo_uid: int = 2, repo_name: str = "repo",
                  repo_path: Path | None = None, apiary_uid: int = 1) -> None:
        _write_registry(self.apiary, {
            str(apiary_uid): {"name": "apiary", "real_path": str(self.apiary),
                              "uid": apiary_uid, "version": "0.1.0"},
            str(repo_uid): {"name": repo_name, "real_path": str(repo_path or self.repo),
                            "uid": repo_uid, "version": "0.1.0"},
        })

    def _write_pins(self, repo: Path, uid: int, name: str,
                    main_apiary: Path | None = None) -> None:
        state.write_self_pointer(repo, {
            "uid": uid, "name": name, "real_path": str(repo),
        })
        state.write_main_apiary_pointer(repo, {
            "main_apiary_path": str(main_apiary or self.apiary), "main_apiary_uid": 1,
        })

    def test_matching_pins_return_clean(self):
        notes, issues = doctor.check_pins(self.apiary)
        self.assertEqual(issues, [])
        self.assertEqual(notes, [])

    def test_uid_mismatch_is_an_issue(self):
        self._write_pins(self.repo, uid=7, name="repo")
        _, issues = doctor.check_pins(self.apiary)
        self.assertTrue(any("uid" in i and "7" in i for i in issues), issues)

    def test_name_mismatch_is_an_issue(self):
        self._write_pins(self.repo, uid=2, name="renamed")
        _, issues = doctor.check_pins(self.apiary)
        self.assertTrue(any("name" in i for i in issues), issues)

    def test_main_apiary_pointer_elsewhere_is_an_issue(self):
        self._write_pins(self.repo, uid=2, name="repo",
                         main_apiary=self.root / "some-other-clone")
        _, issues = doctor.check_pins(self.apiary)
        self.assertTrue(any("main-apiary-pointer" in i for i in issues), issues)

    def test_missing_pins_are_a_note_not_an_issue(self):
        # Lazily-registered repos (state.resolve_target_state_dir) have a
        # registry entry and no pin files; that is a setup step, not drift.
        shutil.rmtree(state.pin_dir(self.repo))
        notes, issues = doctor.check_pins(self.apiary)
        self.assertEqual(issues, [])
        self.assertTrue(any("self-pointer" in n for n in notes), notes)

    def test_unreachable_repo_is_skipped(self):
        self._register(repo_path=self.root / "gone")
        notes, issues = doctor.check_pins(self.apiary)
        self.assertEqual((notes, issues), ([], []))

    def test_main_apiary_registered_under_another_uid_is_an_issue(self):
        # Bug 5: `drift`, `cascade` and `install` all hardcode "uid 1 is
        # main-apiary", and the drift branch it selects rewrites other repos'
        # pointers — so a uid-1 that is not main-apiary is a live hazard.
        self._register(apiary_uid=3, repo_uid=1, repo_name="repo")
        self._write_pins(self.apiary, uid=3, name="apiary")
        self._write_pins(self.repo, uid=1, name="repo")
        _, issues = doctor.check_pins(self.apiary)
        self.assertTrue(any("uid 1" in i for i in issues), issues)

    def test_unregistered_main_apiary_is_a_note(self):
        _write_registry(self.apiary, {
            "2": {"name": "repo", "real_path": str(self.repo), "uid": 2, "version": "0.1.0"},
        })
        notes, issues = doctor.check_pins(self.apiary)
        self.assertEqual(issues, [])
        self.assertTrue(any("self-bootstrap" in n for n in notes), notes)


class FixPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _write_registry(self.apiary, {
            "1": {"name": "apiary", "real_path": str(self.apiary), "uid": 1, "version": "0.1.0"},
            "2": {"name": "repo", "real_path": str(self.repo), "uid": 2, "version": "0.1.0"},
        })
        state.write_self_pointer(self.apiary, {
            "uid": 1, "name": "apiary", "real_path": str(self.apiary),
        })
        state.write_main_apiary_pointer(self.apiary, {
            "main_apiary_path": str(self.apiary), "main_apiary_uid": 1,
        })

    def test_fix_rewrites_the_pins_from_the_registry(self):
        state.write_self_pointer(self.repo, {
            "uid": 9, "name": "old-name", "real_path": str(self.repo),
            "last_drift_check": "2020-01-01T00:00:00Z",
        })
        state.write_main_apiary_pointer(self.repo, {
            "main_apiary_path": str(self.root / "elsewhere"), "main_apiary_uid": 1,
        })
        rc = doctor.main(["pins", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 0)
        sp = state.read_self_pointer(self.repo)
        self.assertEqual(sp["uid"], 2)
        self.assertEqual(sp["name"], "repo")
        self.assertEqual(sp["last_drift_check"], "2020-01-01T00:00:00Z")
        self.assertEqual(
            Path(state.read_main_apiary_pointer(self.repo)["main_apiary_path"]),
            self.apiary.resolve(),
        )
        self.assertEqual(doctor.check_pins(self.apiary)[1], [])

    def test_fix_is_a_no_op_when_a_repo_has_no_pins(self):
        # The repair there is `apiary install`, not a rewrite — and a repo
        # that was never bootstrapped is a note, so the run still passes.
        rc = doctor.main(["pins", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 0)
        self.assertIsNone(state.read_self_pointer(self.repo))

    def test_fix_still_fails_on_an_issue_it_cannot_repair(self):
        # A uid-1 that is not main-apiary needs a human decision (which repo
        # keeps the uid), so --fix reports it and exits 1.
        _write_registry(self.apiary, {
            "1": {"name": "repo", "real_path": str(self.repo), "uid": 1, "version": "0.1.0"},
            "3": {"name": "apiary", "real_path": str(self.apiary), "uid": 3, "version": "0.1.0"},
        })
        state.write_self_pointer(self.apiary, {
            "uid": 3, "name": "apiary", "real_path": str(self.apiary),
        })
        state.write_self_pointer(self.repo, {
            "uid": 1, "name": "repo", "real_path": str(self.repo),
        })
        state.write_main_apiary_pointer(self.repo, {
            "main_apiary_path": str(self.apiary), "main_apiary_uid": 1,
        })
        rc = doctor.main(["pins", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 1)


class CheckCompassTests(unittest.TestCase):
    """`doctor compass` is report-only: notes always, issues never."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_unregistered_apiary_reports_a_note_not_an_issue(self):
        notes, issues = doctor.check_compass(self.apiary)
        self.assertEqual(issues, [])
        self.assertTrue(any("compass" in n for n in notes))

    def test_reports_the_facts_for_a_registered_state_dir(self):
        state.write_self_pointer(self.apiary, {
            "uid": 1, "name": "apiary", "real_path": str(self.apiary),
        })
        state.write_main_apiary_pointer(self.apiary, {
            "main_apiary_path": str(self.apiary), "main_apiary_uid": 1,
        })
        state_dir = state.repos_dir(self.apiary) / "apiary-1"
        (state_dir / "compass" / "observations").mkdir(parents=True)
        (state_dir / "compass" / "observations" / "aaaa0001.json").write_text(
            "{}", encoding="utf-8")
        notes, issues = doctor.check_compass(self.apiary)
        self.assertEqual(issues, [])
        joined = " ".join(notes)
        self.assertIn("observations: 1 active", joined)
        self.assertIn("A/B:", joined)

    def test_compass_is_in_the_all_checks_run(self):
        self.assertIn("compass", doctor.CHECKS)
        self.assertEqual(doctor.main(["compass", "--apiary-repo", str(self.apiary)]), 0)


if __name__ == "__main__":
    unittest.main()
