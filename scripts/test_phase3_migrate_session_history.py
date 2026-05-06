"""Tests for ``scripts/phase3_migrate_session_history.py``."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase3_migrate_session_history as mig


def _make_apiary(root: Path) -> Path:
    apiary = root / "apiary"
    apiary.mkdir()
    (apiary / ".repos").mkdir()
    return apiary


def _write_global_history(global_dir: Path, entries: list[dict]) -> Path:
    p = global_dir / ".session-history.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return p


def _write_registry(apiary: Path, registry: dict) -> None:
    (apiary / ".repos" / "registry.json").write_text(
        json.dumps(registry, indent=2), encoding="utf-8",
    )


class BucketEntriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()

    def test_entries_route_to_matching_repo(self):
        # Note: the project key is derived from real_path (a Windows path
        # gives "D--Professional-..."). Use Windows-style paths so the
        # transcript_path matches Windows project-key convention.
        registry = {
            "1": {"name": "claude-apiary", "real_path": "D:\\Professional\\claude-apiary",
                  "uid": 1, "version": "0.1.0"},
            "3": {"name": "HexWorld", "real_path": "D:\\Professional\\HexWorld",
                  "uid": 3, "version": "0.1.0"},
        }
        entries = [
            {"session_id": "s1", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--Professional-claude-apiary\\s1.jsonl"},
            {"session_id": "s2", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--Professional-HexWorld\\s2.jsonl"},
            {"session_id": "s3", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--Professional-Other\\s3.jsonl"},
        ]
        buckets, orphans = mig._bucket_entries(entries, registry)
        self.assertEqual(len(buckets["claude-apiary-1"]), 1)
        self.assertEqual(buckets["claude-apiary-1"][0]["session_id"], "s1")
        self.assertEqual(len(buckets["HexWorld-3"]), 1)
        self.assertEqual(buckets["HexWorld-3"][0]["session_id"], "s2")
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["session_id"], "s3")

    def test_per_repo_history_uses_v1_schema_wrapper(self):
        registry = {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        }
        _write_registry(self.apiary, registry)
        _write_global_history(self.global_dir, [
            {"session_id": "s1", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--repos-x\\s1.jsonl"},
        ])
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        per_repo = self.apiary / ".repos" / "x-1" / "sessions" / "history.json"
        data = json.loads(per_repo.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["sessions"]), 1)

    def test_orphans_archived(self):
        registry = {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        }
        _write_registry(self.apiary, registry)
        _write_global_history(self.global_dir, [
            {"session_id": "s1", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--repos-other\\s1.jsonl"},
        ])
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        archive = self.apiary / ".apiary" / "legacy" / "orphan-session-history.json"
        self.assertTrue(archive.is_file())
        orphans = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["session_id"], "s1")

    def test_idempotent_rerun_no_dupes(self):
        registry = {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        }
        _write_registry(self.apiary, registry)
        _write_global_history(self.global_dir, [
            {"session_id": "s1", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--repos-x\\s1.jsonl"},
        ])
        for _ in range(2):
            mig.main([
                "--apply",
                "--apiary-repo", str(self.apiary),
                "--global-dir", str(self.global_dir),
            ])
        per_repo = self.apiary / ".repos" / "x-1" / "sessions" / "history.json"
        data = json.loads(per_repo.read_text(encoding="utf-8"))
        self.assertEqual(len(data["sessions"]), 1)  # not duplicated

    def test_dry_run_does_not_write(self):
        registry = {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        }
        _write_registry(self.apiary, registry)
        _write_global_history(self.global_dir, [
            {"session_id": "s1", "transcript_path": "C:\\Users\\u\\.claude\\projects\\D--repos-x\\s1.jsonl"},
        ])
        rc = mig.main([
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        per_repo = self.apiary / ".repos" / "x-1" / "sessions" / "history.json"
        self.assertFalse(per_repo.is_file())

    def test_no_global_history_returns_zero(self):
        # No file at all
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
