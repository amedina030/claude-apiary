"""Unit tests for gui.composer_state — chat input height persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gui import composer_state


class ComposerStateTests(unittest.TestCase):
    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            self.assertEqual(composer_state.load(p), composer_state.DEFAULT_HEIGHT)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            self.assertTrue(composer_state.save(180, p))
            self.assertEqual(composer_state.load(p), 180)

    def test_malformed_json_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertEqual(composer_state.load(p), composer_state.DEFAULT_HEIGHT)

    def test_non_dict_payload_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            p.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(composer_state.load(p), composer_state.DEFAULT_HEIGHT)

    def test_below_min_height_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            p.write_text(
                json.dumps({"height_px": composer_state.MIN_HEIGHT - 1}),
                encoding="utf-8",
            )
            self.assertEqual(composer_state.load(p), composer_state.DEFAULT_HEIGHT)

    def test_above_max_height_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            p.write_text(
                json.dumps({"height_px": composer_state.MAX_HEIGHT + 1}),
                encoding="utf-8",
            )
            self.assertEqual(composer_state.load(p), composer_state.DEFAULT_HEIGHT)

    def test_save_rejects_non_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            self.assertFalse(composer_state.save("tall", p))
            self.assertFalse(p.exists())

    def test_save_rejects_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            self.assertFalse(composer_state.save(0, p))
            self.assertFalse(composer_state.save(composer_state.MAX_HEIGHT + 1, p))
            self.assertFalse(p.exists())

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nested" / "deeper" / "composer_state.json"
            self.assertTrue(composer_state.save(120, p))
            self.assertTrue(p.is_file())
            self.assertEqual(composer_state.load(p), 120)

    def test_float_height_coerced_to_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "composer_state.json"
            self.assertTrue(composer_state.save(199.7, p))
            self.assertEqual(composer_state.load(p), 199)


if __name__ == "__main__":
    unittest.main()
