"""Tests for queue.load_harden_verdict — the helper that reads the runner
harden artifact to expose its verdict in the review queue."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import queue


class LoadHardenVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hardens_dir = Path(self.tmp.name) / "hardens"
        self.hardens_dir.mkdir()
        self._patcher = mock.patch.object(queue, "HARDENS_DIR", self.hardens_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmp.cleanup()

    def _write(self, uuid: str, payload: dict):
        (self.hardens_dir / f"{uuid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_empty_uuid_returns_na(self):
        self.assertEqual(queue.load_harden_verdict(""), "n/a")

    def test_missing_artifact_returns_na(self):
        self.assertEqual(queue.load_harden_verdict("nonexistent"), "n/a")

    def test_reads_verdict_field(self):
        self._write("abc123", {"verdict": "all_resolved"})
        self.assertEqual(queue.load_harden_verdict("abc123"), "all_resolved")

    def test_surfaces_defender_failed(self):
        self._write("abc123", {"verdict": "defender_failed"})
        self.assertEqual(queue.load_harden_verdict("abc123"), "defender_failed")

    def test_missing_verdict_field_returns_unknown(self):
        self._write("abc123", {"uuid": "abc123"})
        self.assertEqual(queue.load_harden_verdict("abc123"), "unknown")

    def test_malformed_json_returns_unknown(self):
        (self.hardens_dir / "abc123.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(queue.load_harden_verdict("abc123"), "unknown")


if __name__ == "__main__":
    unittest.main()
