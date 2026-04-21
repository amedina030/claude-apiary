"""Unit tests for gui.tabs_state — the open-tabs persistence layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gui import tabs_state
from gui.tabs_state import TabEntry


class TabsStateTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tabs.json"
            entries, idx = tabs_state.load(p)
            self.assertEqual(entries, [])
            self.assertEqual(idx, -1)

    def test_round_trip_with_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            cwd_a = Path(tmp) / "a"
            cwd_b = Path(tmp) / "b"
            cwd_a.mkdir()
            cwd_b.mkdir()
            tabs_state.save(
                [
                    TabEntry(cwd=cwd_a, accept_edits=True, allow_self_edits=False),
                    TabEntry(cwd=cwd_b, accept_edits=False, allow_self_edits=True),
                ],
                1,
                state_file,
            )
            entries, idx = tabs_state.load(state_file)
            self.assertEqual([e.cwd for e in entries], [cwd_a, cwd_b])
            self.assertEqual(entries[0].accept_edits, True)
            self.assertEqual(entries[0].allow_self_edits, False)
            self.assertEqual(entries[1].accept_edits, False)
            self.assertEqual(entries[1].allow_self_edits, True)
            self.assertEqual(idx, 1)

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tabs.json"
            p.write_text("{ not json", encoding="utf-8")
            entries, idx = tabs_state.load(p)
            self.assertEqual(entries, [])
            self.assertEqual(idx, -1)

    def test_nonexistent_cwd_filtered_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            real = Path(tmp) / "real"
            real.mkdir()
            fake = Path(tmp) / "ghost-that-never-existed"
            state_file.write_text(
                json.dumps({
                    "tabs": [
                        {"cwd": str(real)},
                        {"cwd": str(fake)},
                    ],
                    "active_idx": 1,
                }),
                encoding="utf-8",
            )
            entries, idx = tabs_state.load(state_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].cwd, real)
            self.assertEqual(idx, 0)

    def test_active_idx_clamped_when_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            d = Path(tmp) / "d"
            d.mkdir()
            state_file.write_text(
                json.dumps({"tabs": [{"cwd": str(d)}], "active_idx": 99}),
                encoding="utf-8",
            )
            entries, idx = tabs_state.load(state_file)
            self.assertEqual(idx, 0)

    def test_empty_tabs_list_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            state_file.write_text(
                json.dumps({"tabs": [], "active_idx": 0}),
                encoding="utf-8",
            )
            entries, idx = tabs_state.load(state_file)
            self.assertEqual(entries, [])
            self.assertEqual(idx, -1)

    def test_legacy_string_entries_upgrade(self):
        """Old string-only entries (pre T-2026-176) load with default settings."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            d = Path(tmp) / "d"
            d.mkdir()
            state_file.write_text(
                json.dumps({"tabs": [str(d)], "active_idx": 0}),
                encoding="utf-8",
            )
            entries, idx = tabs_state.load(state_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].cwd, d)
            self.assertFalse(entries[0].accept_edits)
            self.assertFalse(entries[0].allow_self_edits)
            self.assertEqual(idx, 0)

    def test_save_survives_unwritable_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ok"
            d.mkdir()
            # Should not raise.
            tabs_state.save([TabEntry(cwd=d)], 0, Path(tmp) / "tabs.json")


if __name__ == "__main__":
    unittest.main()
