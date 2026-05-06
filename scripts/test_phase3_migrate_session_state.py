"""Tests for ``scripts/phase3_migrate_session_state.py``."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase3_migrate_session_state as mig


def _make_apiary(root: Path) -> Path:
    apiary = root / "apiary"
    apiary.mkdir()
    (apiary / ".repos").mkdir()
    return apiary


def _seed_per_repo_history(apiary: Path, slug: str, sessions: list[dict]) -> None:
    p = apiary / ".repos" / slug / "sessions" / "history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "sessions": sessions}, indent=2),
                 encoding="utf-8")


def _seed_registry(apiary: Path, registry: dict) -> None:
    (apiary / ".repos" / "registry.json").write_text(
        json.dumps(registry), encoding="utf-8",
    )


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()
        _seed_registry(self.apiary, {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        })
        # Per-repo history with a session whose sid starts with `abcd1234`
        _seed_per_repo_history(self.apiary, "x-1", [
            {"session_id": "abcd1234-5678-9abc-def0-123456789abc",
             "transcript_path": "C:\\u\\.claude\\projects\\D--repos-x\\abcd1234.jsonl"},
        ])

    def test_identity_file_routed_by_short_prefix(self):
        # Identity filename uses the short (first 8 chars) form
        identity = self.global_dir / ".session-identity-abcd1234.json"
        identity.write_text('{"role":"user"}', encoding="utf-8")

        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        # File copied (not moved — identity files stay safe)
        dest = self.apiary / ".repos" / "x-1" / "sessions" / "identity-abcd1234.json"
        self.assertTrue(dest.is_file())
        self.assertTrue(identity.is_file())  # original preserved

    def test_unrouted_identity_archived(self):
        identity = self.global_dir / ".session-identity-deadbeef.json"
        identity.write_text('{}', encoding="utf-8")
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        archive = self.apiary / ".apiary" / "legacy" / "orphan-session-state.json"
        self.assertTrue(archive.is_file())
        data = json.loads(archive.read_text(encoding="utf-8"))
        kinds = [(e["kind"], Path(e["path"]).name) for e in data]
        self.assertIn(("identity", ".session-identity-deadbeef.json"), kinds)


class TranscriptMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()
        (self.global_dir / "transcripts").mkdir()
        _seed_registry(self.apiary, {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        })

    def test_transcript_routed_by_full_sid(self):
        sid = "abcd1234-5678-9abc-def0-123456789abc"
        _seed_per_repo_history(self.apiary, "x-1", [
            {"session_id": sid, "transcript_path": "C:\\u\\.claude\\projects\\D--repos-x\\x.jsonl"},
        ])
        # Global transcript file uses the full sid as filename
        src = self.global_dir / "transcripts" / f"{sid}.jsonl"
        src.write_text("line1\n", encoding="utf-8")

        rc = mig.main([
            "--apply", "--copy-transcripts",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        dest = self.apiary / ".repos" / "x-1" / "sessions" / "transcripts" / f"{sid}.jsonl"
        self.assertTrue(dest.is_file())
        # --copy-transcripts means original is preserved
        self.assertTrue(src.is_file())

    def test_default_move_removes_original(self):
        sid = "feeddead-1111-2222-3333-444455556666"
        _seed_per_repo_history(self.apiary, "x-1", [
            {"session_id": sid, "transcript_path": "C:\\u\\.claude\\projects\\D--repos-x\\x.jsonl"},
        ])
        src = self.global_dir / "transcripts" / f"{sid}.jsonl"
        src.write_text("line1\n", encoding="utf-8")
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        self.assertFalse(src.is_file())  # moved, not copied
        dest = self.apiary / ".repos" / "x-1" / "sessions" / "transcripts" / f"{sid}.jsonl"
        self.assertTrue(dest.is_file())


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()
        _seed_registry(self.apiary, {
            "1": {"name": "x", "real_path": "D:\\repos\\x", "uid": 1, "version": "0.1.0"},
        })

    def test_dry_run_does_not_write(self):
        _seed_per_repo_history(self.apiary, "x-1", [
            {"session_id": "abcd1234-...",
             "transcript_path": "C:\\u\\.claude\\projects\\D--repos-x\\x.jsonl"},
        ])
        identity = self.global_dir / ".session-identity-abcd1234.json"
        identity.write_text('{}', encoding="utf-8")
        rc = mig.main([
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        dest = self.apiary / ".repos" / "x-1" / "sessions" / "identity-abcd1234.json"
        self.assertFalse(dest.is_file())


if __name__ == "__main__":
    unittest.main()
