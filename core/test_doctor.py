"""Tests for ``core/doctor.py`` — read-only consistency checks."""
from __future__ import annotations

import json
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
        self.assertTrue(any("apiary install --target" in i for i in issues))

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
        _write_registry(self.apiary, {
            "1": {"name": "x", "real_path": str(repo), "uid": 1, "version": "0.1.0"},
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


if __name__ == "__main__":
    unittest.main()
