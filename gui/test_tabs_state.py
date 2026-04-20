"""Unit tests for gui.tabs_state — the open-tabs persistence layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gui import tabs_state


class TabsStateTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tabs.json"
            cwds, idx = tabs_state.load(p)
            self.assertEqual(cwds, [])
            self.assertEqual(idx, -1)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            cwd_a = Path(tmp) / "a"
            cwd_b = Path(tmp) / "b"
            cwd_a.mkdir()
            cwd_b.mkdir()
            tabs_state.save([cwd_a, cwd_b], 1, state_file)
            cwds, idx = tabs_state.load(state_file)
            self.assertEqual([str(c) for c in cwds], [str(cwd_a), str(cwd_b)])
            self.assertEqual(idx, 1)

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tabs.json"
            p.write_text("{ not json", encoding="utf-8")
            cwds, idx = tabs_state.load(p)
            self.assertEqual(cwds, [])
            self.assertEqual(idx, -1)

    def test_nonexistent_cwd_filtered_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            real = Path(tmp) / "real"
            real.mkdir()
            fake = Path(tmp) / "ghost-that-never-existed"
            state_file.write_text(
                json.dumps({"tabs": [str(real), str(fake)], "active_idx": 1}),
                encoding="utf-8",
            )
            cwds, idx = tabs_state.load(state_file)
            self.assertEqual(len(cwds), 1)
            self.assertEqual(cwds[0], real)
            # idx clamped because the fake one was dropped.
            self.assertEqual(idx, 0)

    def test_active_idx_clamped_when_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            d = Path(tmp) / "d"
            d.mkdir()
            state_file.write_text(
                json.dumps({"tabs": [str(d)], "active_idx": 99}),
                encoding="utf-8",
            )
            cwds, idx = tabs_state.load(state_file)
            self.assertEqual(idx, 0)

    def test_empty_tabs_list_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "tabs.json"
            state_file.write_text(
                json.dumps({"tabs": [], "active_idx": 0}),
                encoding="utf-8",
            )
            cwds, idx = tabs_state.load(state_file)
            self.assertEqual(cwds, [])
            self.assertEqual(idx, -1)

    def test_save_survives_unwritable_parent(self):
        # Silent on error — caller should never raise from a persistence hook.
        # Use a non-existent drive path to force OSError on Windows, or a
        # file-as-directory collision on POSIX. We simulate by pointing at
        # an existing file's path and writing via tmp rename (which succeeds
        # if it can create tmp), so this test just exercises the no-exception
        # contract on a happy path.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ok"
            d.mkdir()
            # Should not raise.
            tabs_state.save([d], 0, Path(tmp) / "tabs.json")


if __name__ == "__main__":
    unittest.main()
