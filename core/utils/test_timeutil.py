"""Tests for core/utils/timeutil.py — the one on-disk timestamp format."""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils.timeutil import ISO_FORMAT, now_iso

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


if __name__ == "__main__":
    unittest.main()
