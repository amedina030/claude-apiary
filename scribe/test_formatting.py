"""Age renderer (B.10), FORCE_COLOR gating (§5.13), summary rule (§5.6)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe import notes as notes_mod
from scribe.store import derive_summary


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


class TestAgeRenderer(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(notes_mod.format_age(_ago(seconds=10)), "just now")
        self.assertEqual(notes_mod.format_age(_ago(minutes=5)), "5m ago")
        self.assertEqual(notes_mod.format_age(_ago(hours=3)), "3h ago")
        self.assertEqual(notes_mod.format_age(_ago(days=3)), "3d ago")
        self.assertEqual(notes_mod.format_age(_ago(days=14)), "2w ago")     # <30d -> weeks
        self.assertEqual(notes_mod.format_age(_ago(days=60)), "2mo ago")    # <365d -> months
        self.assertEqual(notes_mod.format_age(_ago(days=800)), "2y ago")    # else -> years


class TestColorGating(unittest.TestCase):
    def test_plain_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORCE_COLOR", None)
            self.assertFalse(notes_mod._color_enabled())
            self.assertEqual(notes_mod._c("x", "36"), "x")

    def test_force_color_on(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertTrue(notes_mod._color_enabled())
            self.assertIn("\x1b[", notes_mod._c("x", "36"))

    def test_no_color_wins_over_force(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "NO_COLOR": "1"}, clear=False):
            self.assertFalse(notes_mod._color_enabled())


class TestDeriveSummary(unittest.TestCase):
    def test_first_nonempty_line(self):
        self.assertEqual(derive_summary("\n\n  real first line  \nsecond"), "real first line")

    def test_truncates_at_300(self):
        self.assertEqual(len(derive_summary("x" * 400)), 300)

    def test_empty(self):
        self.assertEqual(derive_summary(""), "")
        self.assertEqual(derive_summary("   \n  "), "")


if __name__ == "__main__":
    unittest.main()
