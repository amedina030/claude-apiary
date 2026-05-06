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
    (apiary / ".apiary" / "forwarding").mkdir(parents=True)
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
        self.assertTrue(any("apiary update" in i for i in issues))

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


class CheckMailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_empty_mailbox_returns_clean(self):
        notes, issues = doctor.check_mailbox(self.apiary)
        self.assertEqual(notes, [])
        self.assertEqual(issues, [])

    def test_pending_message_is_reported(self):
        forwarding = self.apiary / ".apiary" / "forwarding"
        (forwarding / "7.json").write_text("{}", encoding="utf-8")
        _, issues = doctor.check_mailbox(self.apiary)
        self.assertTrue(any("1 pending forwarding" in i for i in issues))


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


if __name__ == "__main__":
    unittest.main()
