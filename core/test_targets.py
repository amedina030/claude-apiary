"""Tests for the apiary targets list/verify CLI."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import targets
from core.utils import state


class TargetsCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = self.root / "apiary"
        self.apiary.mkdir()
        # Patch resolver to point at our fake apiary
        self._resolve_patch = mock.patch.object(
            state, "resolve_apiary_repo", return_value=self.apiary,
        )
        self._resolve_patch.start()
        self.addCleanup(self._resolve_patch.stop)

    def _seed_registry(self, entries: dict) -> None:
        state.repos_dir(self.apiary).mkdir(parents=True, exist_ok=True)
        state._save_registry(self.apiary, entries)

    # --- list ---------------------------------------------------------

    def test_list_empty_registry_prints_friendly_message(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = targets.main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("No registered targets", buf.getvalue())

    def test_list_prints_one_row_per_entry(self):
        live = self.root / "live_target"
        live.mkdir()
        self._seed_registry({
            "1": {
                "name": "live_target", "real_path": str(live),
                "registered_at": "2026-05-01T00:00:00Z",
                "last_used": "2026-05-01T00:00:00Z",
                "verified_ok": True,
            },
            "2": {
                "name": "ghost", "real_path": str(self.root / "deleted"),
                "registered_at": "2026-05-01T00:00:00Z",
                "last_used": "2026-05-01T00:00:00Z",
                "verified_ok": False,
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = targets.main(["list"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("live_target", out)
        self.assertIn("ghost", out)
        self.assertIn("ok", out)
        self.assertIn("MISSING", out)

    # --- verify -------------------------------------------------------

    def test_verify_flips_flag_for_missing_paths(self):
        live = self.root / "live"
        live.mkdir()
        self._seed_registry({
            "1": {
                "name": "live", "real_path": str(live),
                "registered_at": "2026-05-01T00:00:00Z",
                "last_used": "2026-05-01T00:00:00Z",
                "verified_ok": False,  # was wrong, will be corrected
            },
            "2": {
                "name": "ghost", "real_path": str(self.root / "no_such_path"),
                "registered_at": "2026-05-01T00:00:00Z",
                "last_used": "2026-05-01T00:00:00Z",
                "verified_ok": True,  # was wrong, will be corrected
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = targets.main(["verify"])
        self.assertEqual(rc, 0)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertTrue(registry["1"]["verified_ok"])
        self.assertFalse(registry["2"]["verified_ok"])
        # last_verified stamped on every entry
        self.assertIn("last_verified", registry["1"])
        self.assertIn("last_verified", registry["2"])
        # Failure surfaced in output
        out = buf.getvalue()
        self.assertIn("ghost", out)
        self.assertIn("missing", out.lower())

    def test_verify_does_not_delete_missing_entries(self):
        self._seed_registry({
            "1": {
                "name": "ghost", "real_path": str(self.root / "no_such_path"),
                "registered_at": "2026-05-01T00:00:00Z",
                "last_used": "2026-05-01T00:00:00Z",
                "verified_ok": True,
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            targets.main(["verify"])
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertIn("1", registry)
        self.assertFalse(registry["1"]["verified_ok"])

    def test_verify_empty_registry(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = targets.main(["verify"])
        self.assertEqual(rc, 0)
        self.assertIn("No registered targets", buf.getvalue())

    # --- bad invocation -----------------------------------------------

    def test_main_with_no_subcommand_returns_nonzero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = targets.main([])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
