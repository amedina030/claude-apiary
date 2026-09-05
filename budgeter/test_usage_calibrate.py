#!/usr/bin/env python3
"""Tests for budgeter/usage_calibrate.py: pairing samples into intervals,
attributing transcript load, fitting, and describing the open window."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["APIARY_BUDGETER_TEST_ISOLATION"] = "1"

from budgeter import usage_calibrate  # noqa: E402
from budgeter.lib import transcripts, usage_samples  # noqa: E402
from budgeter.test_transcripts import asst_rec, usage, user_rec, write_jsonl  # noqa: E402

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
R1 = "2026-09-05T14:00:00Z"  # window that resets at 14:00, so it opened at 09:00
R2 = "2026-09-05T19:30:00Z"
CWD = "D:\\Professional\\alpha"


def sample(ts, util, resets, source="hook"):
    return {
        "ts": ts.isoformat(),
        "source": source,
        "five_hour": {"utilization": util, "resets_at": resets},
        "seven_day": {"utilization": 1.0, "resets_at": "2026-09-12T00:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
    }


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / "projects"
        self.samples = root / "usage_samples.jsonl"
        # Two sessions: one whose calls land inside the first interval, one
        # after the reset whose single call (14:45) falls inside both the last
        # sampled interval (13:30-15:00) and the open window (14:30-19:30).
        write_jsonl(
            self.projects / "D--Professional-alpha" / "s1.jsonl",
            [
                user_rec(NOW - timedelta(hours=2), "work", CWD),
                asst_rec(
                    NOW - timedelta(minutes=50),
                    "a1",
                    "claude-sonnet-5",
                    usage(0, 0, 0, 1_000_000),
                    CWD,
                ),
                asst_rec(
                    NOW - timedelta(minutes=40),
                    "a2",
                    "claude-sonnet-5",
                    usage(0, 0, 0, 1_000_000),
                    CWD,
                ),
            ],
        )
        write_jsonl(
            self.projects / "D--Professional-alpha" / "s2.jsonl",
            [
                user_rec(NOW + timedelta(hours=2), "later", CWD),
                asst_rec(
                    NOW + timedelta(hours=2, minutes=45),
                    "b1",
                    "claude-sonnet-5",
                    usage(0, 0, 0, 500_000),
                    CWD,
                ),
            ],
        )
        recs = [
            sample(NOW - timedelta(hours=1), 10.0, R1),  # t0
            sample(NOW, 30.0, R1),  # t1: +20% with 20 load (2M sonnet output * 10)
            sample(NOW + timedelta(hours=1), 5.0, R2),  # reset: pair skipped
            sample(NOW + timedelta(hours=1, minutes=30), 8.0, R2),  # +3% with no local calls
            sample(NOW + timedelta(hours=3), 12.0, R2),  # +4% with s2's 5 load
        ]
        self.samples.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        self.weights = transcripts.Weights.from_config({})

    def tearDown(self):
        self._tmp.cleanup()

    def _args(self, *extra):
        return usage_calibrate.build_parser().parse_args(
            [
                "--samples",
                str(self.samples),
                "--projects-dir",
                str(self.projects),
                "--since",
                "2026-09-01",
                *extra,
            ]
        )

    def test_intervals_skip_resets_and_missing_windows(self):
        recs = list(usage_samples.iter_samples(self.samples))
        intervals = usage_calibrate.build_intervals(recs, "five_hour")
        self.assertEqual([round(iv["delta"], 1) for iv in intervals], [20.0, 3.0, 4.0])
        recs[1]["five_hour"] = None  # a sample without the window breaks the chain
        self.assertEqual(len(usage_calibrate.build_intervals(recs, "five_hour")), 2)

    def test_attribution_fit_and_unattributed(self):
        result = usage_calibrate.run(self._args())
        fit = result["fit"]
        loads = [round(iv["load"], 3) for iv in fit["intervals"]]
        self.assertEqual(loads, [20.0, 0.0, 5.0])
        self.assertAlmostEqual(fit["fitted_delta_pct"], 24.0)
        self.assertAlmostEqual(fit["fitted_load"], 25.0)
        self.assertAlmostEqual(fit["pct_per_load"], 24.0 / 25.0)
        self.assertAlmostEqual(fit["unattributed_pct"], 3.0)

    def test_open_window_lists_sessions_with_estimates(self):
        result = usage_calibrate.run(self._args("--top", "5"))
        current = result["open_window"]
        self.assertEqual(current["utilization"], 12.0)
        self.assertEqual(current["resets_at"], "2026-09-05T19:30:00+00:00")
        self.assertEqual(current["window_start"], "2026-09-05T14:30:00+00:00")
        self.assertEqual([r["session"].rsplit("/", 1)[-1] for r in current["sessions"]], ["s2"])
        self.assertAlmostEqual(current["sessions"][0]["est_pct"], round(5.0 * 24.0 / 25.0, 2))
        self.assertEqual(current["sessions"][0]["kind"], "interactive")

    def test_text_and_json_rendering(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = usage_calibrate.main(
                [
                    "--samples",
                    str(self.samples),
                    "--projects-dir",
                    str(self.projects),
                    "--since",
                    "2026-09-01",
                ]
            )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("fit:", text)
        self.assertIn("unattributed: 3.0%", text)
        self.assertIn("open window: 12.0%", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            usage_calibrate.main(
                [
                    "--samples",
                    str(self.samples),
                    "--projects-dir",
                    str(self.projects),
                    "--since",
                    "2026-09-01",
                    "--json",
                ]
            )
        data = json.loads(out.getvalue())
        self.assertEqual(data["window"], "five_hour")
        self.assertEqual(len(data["fit"]["intervals"]), 3)

    def test_no_samples_message(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = usage_calibrate.main(
                [
                    "--samples",
                    str(self.samples.with_name("none.jsonl")),
                    "--projects-dir",
                    str(self.projects),
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("no samples yet", out.getvalue())

    def test_seven_day_window_uses_its_own_length(self):
        result = usage_calibrate.run(self._args("--window", "seven_day"))
        current = result["open_window"]
        self.assertEqual(current["window_start"], "2026-09-05T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
