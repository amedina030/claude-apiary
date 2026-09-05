#!/usr/bin/env python3
"""Tests for budgeter/lib/usage_samples.py and the Stop hook's sampler call."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Same guard test_hooks.py sets: nothing here may touch budgeter/data/.
os.environ["APIARY_BUDGETER_TEST_ISOLATION"] = "1"

from budgeter.lib import usage_samples  # noqa: E402

NOW = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
PAYLOAD = {
    "five_hour": {"utilization": 25.0, "resets_at": "2026-09-05T22:50:00Z"},
    "seven_day": {"utilization": 3, "resets_at": "2026-09-12T18:00:00Z"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False},
    "amber_ladder": {"whatever": 1},
}


class TestCompact(unittest.TestCase):
    def test_keeps_windows_only_and_normalises_missing(self):
        c = usage_samples.compact(PAYLOAD)
        self.assertEqual(set(c), set(usage_samples.WINDOWS))
        self.assertEqual(c["five_hour"], {"utilization": 25.0, "resets_at": "2026-09-05T22:50:00Z"})
        self.assertEqual(c["seven_day"]["utilization"], 3.0)
        self.assertIsNone(c["seven_day_opus"])
        self.assertIsNone(c["seven_day_sonnet"])
        self.assertNotIn("amber_ladder", c)

    def test_non_dict_payload_yields_all_none(self):
        self.assertTrue(all(v is None for v in usage_samples.compact("nope").values()))


class TestRecordAndDue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "usage_samples.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_due(self):
        self.assertIsNone(usage_samples.last_sample_ts(self.path))
        self.assertTrue(usage_samples.is_due(300, NOW, self.path))

    def test_record_then_interval_gating(self):
        rec = usage_samples.record_sample(PAYLOAD, "gui", NOW, self.path)
        self.assertEqual(rec["source"], "gui")
        self.assertEqual(rec["five_hour"]["utilization"], 25.0)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["ts"], NOW.isoformat())
        self.assertEqual(usage_samples.last_sample_ts(self.path), NOW)
        self.assertFalse(usage_samples.is_due(300, NOW + timedelta(seconds=100), self.path))
        self.assertTrue(usage_samples.is_due(300, NOW + timedelta(seconds=300), self.path))

    def test_corrupt_tail_line_is_skipped(self):
        usage_samples.record_sample(PAYLOAD, "hook", NOW, self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(usage_samples.last_sample_ts(self.path), NOW)

    def test_record_if_due_uses_existing_payload(self):
        self.assertIsNotNone(usage_samples.record_if_due(PAYLOAD, "gui", 300, NOW, self.path))
        self.assertIsNone(
            usage_samples.record_if_due(PAYLOAD, "gui", 300, NOW + timedelta(seconds=10), self.path)
        )
        self.assertIsNone(
            usage_samples.record_if_due(None, "gui", 300, NOW + timedelta(hours=1), self.path)
        )
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_sample_if_due_fetches_only_when_due(self):
        calls = []

        def fetch():
            calls.append(1)
            return PAYLOAD

        self.assertIsNotNone(usage_samples.sample_if_due("hook", fetch, 300, NOW, self.path))
        self.assertIsNone(
            usage_samples.sample_if_due("hook", fetch, 300, NOW + timedelta(seconds=5), self.path)
        )
        self.assertEqual(len(calls), 1)

    def test_sample_if_due_tolerates_fetch_failure(self):
        self.assertIsNone(usage_samples.sample_if_due("hook", lambda: None, 300, NOW, self.path))

        def boom():
            raise OSError("network down")

        with mock.patch("sys.stderr"):
            self.assertIsNone(usage_samples.sample_if_due("hook", boom, 300, NOW, self.path))
        self.assertFalse(self.path.exists())

    def test_iter_samples_with_since(self):
        usage_samples.record_sample(PAYLOAD, "hook", NOW, self.path)
        usage_samples.record_sample(PAYLOAD, "hook", NOW + timedelta(minutes=10), self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        all_recs = list(usage_samples.iter_samples(self.path))
        self.assertEqual(len(all_recs), 2)
        self.assertEqual(all_recs[0]["_ts"], NOW)
        later = list(usage_samples.iter_samples(self.path, since=NOW + timedelta(minutes=5)))
        self.assertEqual(len(later), 1)
        self.assertEqual(list(usage_samples.iter_samples(self.path / "missing")), [])

    def test_isolation_guard_fires_before_fetch(self):
        calls = []
        with self.assertRaises(RuntimeError):
            usage_samples.sample_if_due("hook", lambda: calls.append(1) or PAYLOAD, 300, NOW, None)
        self.assertEqual(calls, [])
        with self.assertRaises(RuntimeError):
            usage_samples.record_if_due(PAYLOAD, "gui", 300, NOW, None)


class TestInterval(unittest.TestCase):
    def test_reads_config_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"usage_sample_interval_seconds": 42}), encoding="utf-8")
            self.assertEqual(usage_samples.sample_interval_seconds(cfg), 42)
            cfg.write_text(json.dumps({"usage_sample_interval_seconds": -1}), encoding="utf-8")
            self.assertEqual(
                usage_samples.sample_interval_seconds(cfg), usage_samples.DEFAULT_INTERVAL_SECONDS
            )
            cfg.write_text("not json", encoding="utf-8")
            self.assertEqual(
                usage_samples.sample_interval_seconds(cfg), usage_samples.DEFAULT_INTERVAL_SECONDS
            )
            self.assertEqual(
                usage_samples.sample_interval_seconds(Path(tmp) / "missing.json"),
                usage_samples.DEFAULT_INTERVAL_SECONDS,
            )


class TestStopHookSampler(unittest.TestCase):
    def test_kill_switch_and_default_on(self):
        from budgeter.hooks import stop_session

        with (
            mock.patch.object(stop_session.flags, "is_enabled", return_value=True),
            mock.patch.object(usage_samples, "sample_if_due") as sample,
        ):
            stop_session._sample_usage()
        sample.assert_not_called()

        with (
            mock.patch.object(stop_session.flags, "is_enabled", return_value=False),
            mock.patch.object(usage_samples, "sample_if_due") as sample,
        ):
            stop_session._sample_usage()
        sample.assert_called_once()
        self.assertEqual(sample.call_args.args[0], "hook")
        self.assertEqual(sample.call_args.args[1].__name__, "fetch_usage")


if __name__ == "__main__":
    unittest.main()
