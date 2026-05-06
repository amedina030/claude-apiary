"""Tests for ``core/mailbox.py`` — drift forwarding inbox."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import mailbox
from core.utils import state


def _make_apiary(root: Path) -> Path:
    """Minimal main-apiary skeleton — registry + forwarding dir + VERSION."""
    apiary = root / "apiary"
    apiary.mkdir()
    (apiary / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (apiary / ".repos").mkdir()
    (apiary / ".repos" / "registry.json").write_text("{}", encoding="utf-8")
    (apiary / ".apiary" / "forwarding").mkdir(parents=True)
    return apiary


class WriteMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def test_write_update_path_message(self):
        p = mailbox.write_message(
            self.apiary,
            from_uid=7,
            kind=mailbox.KIND_UPDATE_PATH,
            new_path="/new/loc",
            old_path="/old/loc",
            name="HexWorld",
            version="0.1.0",
        )
        self.assertTrue(p.is_file())
        self.assertEqual(p.name, "7.json")
        msg = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(msg["kind"], mailbox.KIND_UPDATE_PATH)
        self.assertEqual(msg["from_uid"], 7)
        self.assertEqual(msg["new_path"], "/new/loc")
        self.assertEqual(msg["old_path"], "/old/loc")
        self.assertEqual(msg["schema_version"], mailbox.MAILBOX_SCHEMA_VERSION)

    def test_write_register_copy_message(self):
        p = mailbox.write_message(
            self.apiary,
            from_uid=12,
            kind=mailbox.KIND_REGISTER_COPY,
            new_path="/copy/loc",
            name="HexWorld",
            version="0.1.0",
        )
        msg = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(msg["kind"], mailbox.KIND_REGISTER_COPY)
        self.assertNotIn("old_path", msg)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            mailbox.write_message(
                self.apiary, from_uid=1, kind="banana",
                new_path="/p", name="x", version="0.1.0",
            )

    def test_overwrite_collapses_to_latest(self):
        """Per §9.6: a second message from the same uid replaces the first."""
        mailbox.write_message(
            self.apiary, from_uid=3, kind=mailbox.KIND_UPDATE_PATH,
            new_path="/path1", name="x", version="0.1.0",
        )
        mailbox.write_message(
            self.apiary, from_uid=3, kind=mailbox.KIND_UPDATE_PATH,
            new_path="/path2", name="x", version="0.1.0",
        )
        msgs = mailbox.list_pending(self.apiary)
        self.assertEqual(len(msgs), 1)
        msg = json.loads(msgs[0].read_text(encoding="utf-8"))
        self.assertEqual(msg["new_path"], "/path2")


class ProcessPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_apiary(self.root)

    def _seed_registry(self, entries: dict) -> None:
        (self.apiary / ".repos" / "registry.json").write_text(
            json.dumps(entries), encoding="utf-8",
        )

    def _seed_next_id(self, value: int) -> None:
        (self.apiary / ".repos" / "next_id").write_text(str(value), encoding="utf-8")

    def test_no_pending_returns_zero_processed(self):
        report = mailbox.process_pending(self.apiary)
        self.assertEqual(report["processed"], 0)
        self.assertEqual(report["applied"], [])

    def test_update_path_applies_and_deletes_message(self):
        self._seed_registry({
            "5": {"name": "x", "real_path": "/old", "uid": 5, "version": "0.1.0",
                  "registered_at": "t", "last_used": "t"},
        })
        mailbox.write_message(
            self.apiary, from_uid=5, kind=mailbox.KIND_UPDATE_PATH,
            new_path="/new", old_path="/old", name="x", version="0.1.0",
        )
        report = mailbox.process_pending(self.apiary)
        self.assertEqual(report["processed"], 1)
        self.assertEqual(len(report["applied"]), 1)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["5"]["real_path"], "/new")
        # Message file deleted
        self.assertFalse(mailbox.message_path(self.apiary, 5).is_file())

    def test_register_copy_creates_new_entry(self):
        self._seed_registry({
            "5": {"name": "x", "real_path": "/orig", "uid": 5, "version": "0.1.0"},
        })
        mailbox.write_message(
            self.apiary, from_uid=8, kind=mailbox.KIND_REGISTER_COPY,
            new_path="/copy", name="x", version="0.1.0",
        )
        report = mailbox.process_pending(self.apiary)
        self.assertEqual(report["processed"], 1)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertIn("8", registry)
        self.assertEqual(registry["8"]["real_path"], "/copy")
        self.assertEqual(registry["8"]["uid"], 8)

    def test_update_path_for_unknown_uid_records_error_and_keeps_file(self):
        # No registry entry for uid 99 — message is left on disk for
        # operator triage rather than silently dropped.
        mailbox.write_message(
            self.apiary, from_uid=99, kind=mailbox.KIND_UPDATE_PATH,
            new_path="/anywhere", name="x", version="0.1.0",
        )
        report = mailbox.process_pending(self.apiary)
        self.assertEqual(report["processed"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("unknown uid", report["errors"][0]["reason"])
        self.assertTrue(mailbox.message_path(self.apiary, 99).is_file())

    def test_malformed_message_recorded_as_error_not_processed(self):
        # Manually write a corrupt message
        bad = mailbox.message_path(self.apiary, 1)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json", encoding="utf-8")
        report = mailbox.process_pending(self.apiary)
        # Malformed messages contribute an error but aren't counted toward processed
        # (they stay on disk for operator triage).
        self.assertEqual(len(report["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
