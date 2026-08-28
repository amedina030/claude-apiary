"""Tests for core/utils/timeutil.py — the one on-disk timestamp format."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils.timeutil import ISO_FORMAT, now_iso, parse_iso

_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class NowIsoTests(unittest.TestCase):
    def test_shape_is_second_precision_utc_with_a_z(self) -> None:
        self.assertRegex(now_iso(), _SHAPE)

    def test_round_trips_through_the_shared_format(self) -> None:
        parsed = datetime.strptime(now_iso(), ISO_FORMAT)
        self.assertIsNone(parsed.tzinfo)  # naive; the Z carries the zone

    def test_value_is_utc_not_local(self) -> None:
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:")
        self.assertTrue(now_iso().startswith(expected[:16]))

    def test_no_microseconds_and_no_offset(self) -> None:
        stamp = now_iso()
        self.assertNotIn(".", stamp)
        self.assertNotIn("+", stamp)

    def test_strings_sort_chronologically(self) -> None:
        # The whole reason for one format: these are compared as text.
        early = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).strftime(ISO_FORMAT)
        late = datetime(2026, 11, 2, 3, 4, 5, tzinfo=timezone.utc).strftime(ISO_FORMAT)
        self.assertLess(early, late)


class ParseIsoTests(unittest.TestCase):
    def test_round_trips_now_iso(self) -> None:
        stamp = now_iso()
        parsed = parse_iso(stamp)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.strftime(ISO_FORMAT), stamp)

    def test_accepts_an_offset_form(self) -> None:
        self.assertEqual(
            parse_iso("2026-08-26T12:00:00+00:00"),
            datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )

    def test_accepts_microseconds(self) -> None:
        # datetime.isoformat() — what scribe's index rows actually carry.
        written = datetime(2026, 8, 26, 12, 0, 0, 123456, tzinfo=timezone.utc).isoformat()
        self.assertEqual(parse_iso(written).microsecond, 123456)

    def test_returns_none_for_junk(self) -> None:
        for value in ("", "not a date", None, 17, [], "2026-13-45T99:99:99Z"):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso(value))


if __name__ == "__main__":
    unittest.main()
