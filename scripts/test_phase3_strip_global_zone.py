"""Tests for ``scripts/phase3_strip_global_zone.py``."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase3_strip_global_zone as mig


_USER_PREAMBLE = "# My Global Rules\n\nDon't break the build.\n\n"
_USER_POSTAMBLE = "\n## My Other Rules\n\nFollow conventions.\n"
_VALID_ZONE = (
    "<!-- apiary-context-rules-start -->\n"
    "\n"
    "<!-- apiary-context-rule:test_rule hash=abc123 -->\n"
    "Some rule body.\n"
    "<!-- /apiary-context-rule:test_rule -->\n"
    "\n"
    "<!-- apiary-context-rules-end -->"
)


class StripZoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "CLAUDE.md"

    def test_no_zone_is_a_noop(self):
        self.target.write_text("Hello\n", encoding="utf-8")
        new_text, removed, reason = mig.strip_zone(self.target.read_text(encoding="utf-8"))
        self.assertFalse(removed)
        self.assertEqual(reason, "no zone")
        self.assertEqual(new_text, "Hello\n")

    def test_zone_stripped_preserves_user_content(self):
        text = _USER_PREAMBLE + _VALID_ZONE + _USER_POSTAMBLE
        new_text, removed, _ = mig.strip_zone(text)
        self.assertTrue(removed)
        self.assertNotIn("apiary-context-rules-start", new_text)
        self.assertIn("Don't break the build", new_text)
        self.assertIn("Follow conventions", new_text)

    def test_main_dry_run_does_not_write(self):
        self.target.write_text(_USER_PREAMBLE + _VALID_ZONE, encoding="utf-8")
        rc = mig.main(["--target", str(self.target)])
        self.assertEqual(rc, 0)
        self.assertIn("apiary-context-rules-start", self.target.read_text(encoding="utf-8"))

    def test_main_apply_writes(self):
        self.target.write_text(_USER_PREAMBLE + _VALID_ZONE, encoding="utf-8")
        rc = mig.main(["--apply", "--target", str(self.target)])
        self.assertEqual(rc, 0)
        self.assertNotIn("apiary-context-rules-start", self.target.read_text(encoding="utf-8"))

    def test_tampered_zone_refuses_without_force(self):
        # Two start markers → tampered
        tampered = (
            "<!-- apiary-context-rules-start -->\n"
            "<!-- apiary-context-rules-start -->\n"
            "<!-- apiary-context-rules-end -->"
        )
        self.target.write_text(tampered, encoding="utf-8")
        rc = mig.main(["--apply", "--target", str(self.target)])
        self.assertEqual(rc, 2)
        # File unchanged
        self.assertIn("apiary-context-rules-start", self.target.read_text(encoding="utf-8"))

    def test_missing_target_file_is_zero_exit(self):
        rc = mig.main(["--apply", "--target", str(self.target)])  # doesn't exist
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
