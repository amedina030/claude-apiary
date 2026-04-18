"""Tests for compass.store — paths, dimensions, validation, archive policy."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from compass import store


class ValidateObservationTest(unittest.TestCase):
    def _payload(self, **overrides):
        base = {
            "session_id": "deadbeef",
            "captured_at": "2026-04-17T20:00:00Z",
            "observations": [
                {
                    "dimension": "communication_style",
                    "observation": "Terse single-line directives.",
                    "evidence": "yep",
                    "volatility": "stable",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_payload(self):
        self.assertEqual(store.validate_observation(self._payload()), [])

    def test_empty_observations_list_is_valid(self):
        self.assertEqual(
            store.validate_observation(self._payload(observations=[])), []
        )

    def test_missing_session_id(self):
        payload = self._payload()
        del payload["session_id"]
        self.assertIn("session_id missing or not a valid 8-char hex / UUID",
                      store.validate_observation(payload))

    def test_session_id_uppercase_hex_rejected(self):
        # Regex requires lowercase hex.
        errors = store.validate_observation(self._payload(session_id="DEADBEEF"))
        self.assertTrue(any("session_id" in e for e in errors))

    def test_filename_match_check_passes(self):
        errors = store.validate_observation(
            self._payload(), expected_session_id="deadbeef"
        )
        self.assertEqual(errors, [])

    def test_filename_match_check_fails(self):
        errors = store.validate_observation(
            self._payload(session_id="deadbeef"),
            expected_session_id="cafebabe",
        )
        self.assertTrue(any("does not match expected" in e for e in errors))

    def test_filename_match_uses_short_prefix(self):
        # Full UUID payload, short prefix expected — should match.
        errors = store.validate_observation(
            self._payload(session_id="deadbeef-1234-5678-9abc-def012345678"),
            expected_session_id="deadbeef",
        )
        self.assertEqual(errors, [])

    def test_invalid_captured_at(self):
        errors = store.validate_observation(self._payload(captured_at="not-a-date"))
        self.assertTrue(any("captured_at" in e for e in errors))

    def test_unknown_dimension_rejected(self):
        payload = self._payload()
        payload["observations"][0]["dimension"] = "telekinesis"
        errors = store.validate_observation(payload)
        self.assertTrue(any("not in configured dimensions" in e for e in errors))

    def test_invalid_volatility_rejected(self):
        payload = self._payload()
        payload["observations"][0]["volatility"] = "spicy"
        errors = store.validate_observation(payload)
        self.assertTrue(any("volatility must be one of" in e for e in errors))

    def test_empty_evidence_rejected(self):
        payload = self._payload()
        payload["observations"][0]["evidence"] = "   "
        errors = store.validate_observation(payload)
        self.assertTrue(any("evidence missing or empty" in e for e in errors))

    def test_payload_not_dict(self):
        self.assertEqual(
            store.validate_observation([1, 2, 3]),
            ["payload is not a JSON object"],
        )


class DimensionsTest(unittest.TestCase):
    def test_load_dimensions_returns_dict(self):
        data = store.load_dimensions()
        self.assertIn("dimensions", data)
        self.assertIsInstance(data["dimensions"], list)
        self.assertGreater(len(data["dimensions"]), 0)

    def test_dimension_names_unique(self):
        names = store.dimension_names()
        self.assertEqual(len(names), len(set(names)))

    def test_volatile_dimension_is_subset(self):
        vol = set(store.volatile_dimension_names())
        all_names = set(store.dimension_names())
        self.assertTrue(vol.issubset(all_names))


class PathsTest(unittest.TestCase):
    def test_observation_path_uses_8char_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            short = store.observation_path("deadbeef", start=tmp_path)
            full = store.observation_path("deadbeef-1234-5678-9abc-def012345678",
                                          start=tmp_path)
            self.assertEqual(short.name, "deadbeef.json")
            self.assertEqual(full.name, "deadbeef.json")

    def test_archive_target_path_uses_iso_year_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = store.observation_path("deadbeef", start=tmp_path)
            dt = datetime(2026, 1, 15, tzinfo=timezone.utc)  # ISO week 3
            dest = store.archive_target_path(src, now=dt, start=tmp_path)
            self.assertEqual(dest.parent.name, "2026-03")
            self.assertEqual(dest.name, "deadbeef.json")


class LoadObservationTest(unittest.TestCase):
    def test_load_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data = {
                "session_id": "deadbeef",
                "captured_at": "2026-04-17T20:00:00Z",
                "observations": [],
            }
            p = tmp_path / "deadbeef.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(store.load_observation(p), data)

    def test_load_invalid_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "broken.json"
            p.write_text("not json", encoding="utf-8")
            self.assertIsNone(store.load_observation(p))

    def test_load_invalid_schema_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"oops": True}), encoding="utf-8")
            self.assertIsNone(store.load_observation(p))


class ListActiveTest(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        # Mimic a git repo so compass_dir resolves under us.
        os.chdir(self.repo)
        # Restore cwd before tempfile cleanup runs (Windows can't rmdir an in-use cwd).
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)

    def test_returns_empty_for_missing_dir(self):
        # No observations dir at all.
        self.assertEqual(store.list_active_observations(start=self.repo), [])
        self.assertEqual(store.count_active_observations(start=self.repo), 0)

    def test_skips_archive_subdir(self):
        obs = store.observations_dir(start=self.repo)
        obs.mkdir(parents=True, exist_ok=True)
        (obs / "active.json").write_text("{}", encoding="utf-8")
        archive = store.archive_dir(start=self.repo)
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "old.json").write_text("{}", encoding="utf-8")
        files = store.list_active_observations(start=self.repo)
        self.assertEqual([p.name for p in files], ["active.json"])


if __name__ == "__main__":
    unittest.main()
