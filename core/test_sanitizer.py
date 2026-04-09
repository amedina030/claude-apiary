"""Tests for core/sanitizer.py.

Test inputs are constructed from byte codes via ``_p()`` so that this
source file, like the sanitizer it tests, stays free of literal trigger
sequences. A dedicated self-check at the bottom enforces that property.
"""
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.sanitizer import sanitize_and_report, sanitize_injected_text


def _p(*codes: int) -> str:
    return bytes(codes).decode("ascii")


class SanitizerTest(unittest.TestCase):
    def test_path_traversal_unix_replaced(self):
        raw = "see " + _p(0x2e, 0x2e, 0x2f) + "etc/passwd for details"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:path-traversal-unix>", out)
        self.assertNotIn(_p(0x2e, 0x2e, 0x2f), out)

    def test_path_traversal_win_replaced(self):
        raw = "file at " + _p(0x2e, 0x2e, 0x5c) + "config"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:path-traversal-win>", out)

    def test_url_encoded_traversal_replaced(self):
        raw = "payload was " + _p(0x25, 0x32, 0x65, 0x25, 0x32, 0x65, 0x25, 0x32, 0x66) + "x"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:url-encoded-traversal>", out)

    def test_destructive_shell_replaced(self):
        raw = "do not run " + _p(0x72, 0x6d, 0x20, 0x2d, 0x72, 0x66) + " /"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:destructive-shell-rm>", out)

    def test_destructive_shell_case_insensitive(self):
        raw = "RM " + _p(0x2d, 0x52, 0x46) + " foo"
        out = sanitize_injected_text(raw)
        # Case-insensitive match on the uppercase variant
        self.assertIn("<scrubbed:destructive-shell-rm>", out.replace("RM", "rm"))

    def test_fork_bomb_replaced(self):
        raw = "the classic " + _p(0x3a, 0x28, 0x29, 0x7b) + " :|:& };:"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:fork-bomb>", out)

    def test_sql_drop_table_replaced(self):
        raw = "never " + _p(0x44, 0x52, 0x4f, 0x50, 0x20, 0x54, 0x41, 0x42, 0x4c, 0x45) + " users"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:sql-drop-table>", out)

    def test_sql_union_select_replaced(self):
        raw = "injection via " + _p(0x75, 0x6e, 0x69, 0x6f, 0x6e, 0x20, 0x73, 0x65, 0x6c, 0x65, 0x63, 0x74) + " 1,2,3"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:sql-union-select>", out)

    def test_xss_script_tag_replaced(self):
        raw = "xss via " + _p(0x3c, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74) + ">alert(1)</script>"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:xss-script-tag>", out)

    def test_xss_javascript_uri_replaced(self):
        raw = "link was " + _p(0x6a, 0x61, 0x76, 0x61, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74, 0x3a) + "alert(1)"
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:xss-javascript-uri>", out)

    def test_clean_text_unchanged(self):
        raw = "just some notes about work and a few todos. Nothing dangerous here."
        self.assertEqual(sanitize_injected_text(raw), raw)

    def test_empty_string_unchanged(self):
        self.assertEqual(sanitize_injected_text(""), "")

    def test_none_passes_through(self):
        self.assertIsNone(sanitize_injected_text(None))

    def test_idempotent(self):
        raw = "danger " + _p(0x2e, 0x2e, 0x2f) + " here and " + _p(0x72, 0x6d, 0x20, 0x2d, 0x72, 0x66) + " too"
        once = sanitize_injected_text(raw)
        twice = sanitize_injected_text(once)
        self.assertEqual(once, twice)

    def test_multiple_patterns_in_one_input(self):
        raw = (
            "first " + _p(0x2e, 0x2e, 0x2f) + " then "
            + _p(0x72, 0x6d, 0x20, 0x2d, 0x72, 0x66) + " and finally "
            + _p(0x3c, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74)
        )
        out = sanitize_injected_text(raw)
        self.assertIn("<scrubbed:path-traversal-unix>", out)
        self.assertIn("<scrubbed:destructive-shell-rm>", out)
        self.assertIn("<scrubbed:xss-script-tag>", out)

    def test_report_counts_single_hit(self):
        raw = "see " + _p(0x2e, 0x2e, 0x2f) + "x"
        scrubbed, hits = sanitize_and_report(raw)
        self.assertEqual(hits, {"path-traversal-unix": 1})
        self.assertIn("<scrubbed:path-traversal-unix>", scrubbed)

    def test_report_counts_multiple_hits_same_label(self):
        raw = _p(0x2e, 0x2e, 0x2f) + " and " + _p(0x2e, 0x2e, 0x2f) + " again"
        _, hits = sanitize_and_report(raw)
        self.assertEqual(hits, {"path-traversal-unix": 2})

    def test_report_counts_multiple_labels(self):
        raw = (
            _p(0x2e, 0x2e, 0x2f) + " and "
            + _p(0x72, 0x6d, 0x20, 0x2d, 0x72, 0x66) + " and "
            + _p(0x3c, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74)
        )
        _, hits = sanitize_and_report(raw)
        self.assertEqual(
            hits,
            {
                "path-traversal-unix": 1,
                "destructive-shell-rm": 1,
                "xss-script-tag": 1,
            },
        )

    def test_report_empty_on_clean_text(self):
        scrubbed, hits = sanitize_and_report("just notes, nothing here")
        self.assertEqual(hits, {})
        self.assertEqual(scrubbed, "just notes, nothing here")

    def test_report_none_input(self):
        scrubbed, hits = sanitize_and_report(None)
        self.assertIsNone(scrubbed)
        self.assertEqual(hits, {})

    def test_source_file_contains_no_literal_triggers(self):
        """Hygiene: sanitizer.py and this test file must not contain literal triggers.

        If they did, reading these files via a file-read tool would feed the
        trigger patterns back into the API and defeat the whole point.
        """
        forbidden = [
            _p(0x2e, 0x2e, 0x2f),
            _p(0x2e, 0x2e, 0x5c),
            _p(0x25, 0x32, 0x65, 0x25, 0x32, 0x65, 0x25, 0x32, 0x66),
            _p(0x72, 0x6d, 0x20, 0x2d, 0x72, 0x66),
            _p(0x3a, 0x28, 0x29, 0x7b),
            _p(0x64, 0x72, 0x6f, 0x70, 0x20, 0x74, 0x61, 0x62, 0x6c, 0x65),
            _p(0x75, 0x6e, 0x69, 0x6f, 0x6e, 0x20, 0x73, 0x65, 0x6c, 0x65, 0x63, 0x74),
            _p(0x3c, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74),
            _p(0x6a, 0x61, 0x76, 0x61, 0x73, 0x63, 0x72, 0x69, 0x70, 0x74, 0x3a),
        ]
        targets = [
            _HERE / "sanitizer.py",
            _HERE / "test_sanitizer.py",
        ]
        for target in targets:
            content = target.read_text(encoding="utf-8").lower()
            for f in forbidden:
                self.assertNotIn(
                    f, content,
                    f"{target.name} contains a literal trigger pattern; "
                    f"construct it via _pat() / _p() instead.",
                )


if __name__ == "__main__":
    unittest.main()
