#!/usr/bin/env python3
"""Tests for core.utils.jsonc — JSONC comment stripper + trailing-comma tolerance."""

import tempfile
import unittest
from pathlib import Path

from core.utils.jsonc import JsoncParseError, load, loads


class TestLoads(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(loads('{"a": 1}'), {"a": 1})

    def test_line_comment(self):
        text = '{\n  "a": 1 // trailing\n}'
        self.assertEqual(loads(text), {"a": 1})

    def test_line_comment_at_start(self):
        text = '// header\n{"a": 1}'
        self.assertEqual(loads(text), {"a": 1})

    def test_block_comment_single_line(self):
        text = '{ /* c */ "a": /* c */ 1 }'
        self.assertEqual(loads(text), {"a": 1})

    def test_block_comment_multi_line(self):
        text = '{\n  /* multi\n     line\n     comment */\n  "a": 1\n}'
        self.assertEqual(loads(text), {"a": 1})

    def test_trailing_comma_object(self):
        self.assertEqual(loads('{"a": 1,}'), {"a": 1})

    def test_trailing_comma_array(self):
        self.assertEqual(loads('{"a": [1, 2, 3,]}'), {"a": [1, 2, 3]})

    def test_trailing_comma_with_whitespace(self):
        self.assertEqual(loads('{"a": [1, 2,\n  \n]}'), {"a": [1, 2]})

    def test_comment_inside_string_preserved(self):
        self.assertEqual(loads('{"a": "// not a comment"}'), {"a": "// not a comment"})

    def test_block_comment_marker_inside_string_preserved(self):
        self.assertEqual(loads('{"a": "/* not */ a comment"}'), {"a": "/* not */ a comment"})

    def test_escaped_quote_in_string(self):
        self.assertEqual(loads('{"a": "say \\"hi\\""}'), {"a": 'say "hi"'})

    def test_comma_inside_string_preserved(self):
        self.assertEqual(loads('{"a": "x,y,z"}'), {"a": "x,y,z"})

    def test_nested_structure(self):
        text = """
        {
            // users
            "users": [
                {"name": "a", /* id */ "id": 1,},
                {"name": "b", "id": 2},
            ],
            "count": 2,
        }
        """
        self.assertEqual(
            loads(text),
            {"users": [{"name": "a", "id": 1}, {"name": "b", "id": 2}], "count": 2},
        )

    def test_malformed_raises_parse_error(self):
        with self.assertRaises(JsoncParseError) as cm:
            loads('{"a": }')
        self.assertIsNotNone(cm.exception.line)

    def test_line_number_reported_on_parse_error(self):
        text = '{\n  // junk\n  "a": ,\n}'
        with self.assertRaises(JsoncParseError) as cm:
            loads(text, path=Path("x.jsonc"))
        self.assertIsNotNone(cm.exception.line)
        self.assertGreaterEqual(cm.exception.line, 3)
        self.assertIn("x.jsonc", str(cm.exception))

    def test_unterminated_block_comment_does_not_infinite_loop(self):
        # We accept the text up to the end rather than loop forever; the
        # resulting JSON is truncated so json.loads raises a parse error.
        with self.assertRaises(JsoncParseError):
            loads('{"a": 1 /* unterminated')


class TestLoadFile(unittest.TestCase):
    def test_load_reads_file_and_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "x.jsonc"
            p.write_text('// hi\n{"a": 1,}', encoding="utf-8")
            self.assertEqual(load(p), {"a": 1})

    def test_load_carries_path_into_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "broken.jsonc"
            p.write_text('{"a": }', encoding="utf-8")
            with self.assertRaises(JsoncParseError) as cm:
                load(p)
            self.assertEqual(cm.exception.path, p)
            self.assertIn("broken.jsonc", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
