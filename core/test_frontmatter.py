"""Tests for the one frontmatter dialect (`core/frontmatter.py`, Phase 3.3).

The round-trip cases are the ones that matter most: ``/research verify`` is
literally ``parse → mutate → dump``, and scribe rewrites a learning's
frontmatter on every retag, so an asymmetry between the reader and the writer
corrupts real files a little more each pass (deep review, knowledge.md §3 2).
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import frontmatter  # noqa: E402


# Values that must survive dump → parse unchanged, in a scalar and in a list.
# The first three are the exact inputs recorded in knowledge.md §3 2; the globs
# and the timestamp are real values from the live store.
HOSTILE_VALUES = (
    "Foo: bar",
    "C# generics",
    "https://example.com/a#frag",
    "http://h:8080/p",
    "issue#12",
    "Claude Code GUI: interactive-wrapper vs Agent-SDK billing",
    'say "hi"',
    "it's fine",
    '"already quoted"',
    "'single quoted'",
    "a\"b'c",
    "trailing hash #",
    "# leading hash",
    "- leading dash",
    "back\\slash",
    "  padded  ",
    "",
    "[]",
    "{}",
    "[a, b]",
    "a, b",
    "ideas/*/0[0-9]-*.md",
    "**/*.py",
    "2026-05-01 22:24:21 (PIE, frame 89095)",
    "What's pending",
    "plain value",
)


class TestScalars(unittest.TestCase):
    def test_every_scalar_loads_as_a_string(self) -> None:
        meta = frontmatter.loads("n: 5\nflag: true\nwhen: 2026-08-26\n")
        self.assertEqual(meta, {"n": "5", "flag": "true", "when": "2026-08-26"})

    def test_symmetric_quotes_are_stripped(self) -> None:
        self.assertEqual(frontmatter.loads('t: "Foo: bar"')["t"], "Foo: bar")
        self.assertEqual(frontmatter.loads("t: 'Foo: bar'")["t"], "Foo: bar")

    def test_value_that_merely_contains_a_quote_is_untouched(self) -> None:
        self.assertEqual(frontmatter.loads('t: say "hi"')["t"], 'say "hi"')
        self.assertEqual(frontmatter.loads("t: it's fine")["t"], "it's fine")

    def test_quotes_inside_a_quoted_value_are_preserved(self) -> None:
        self.assertEqual(frontmatter.loads('t: "he said \\"hi\\""')["t"], 'he said "hi"')
        self.assertEqual(frontmatter.loads("t: 'it''s'")["t"], "it's")

    def test_escape_sequences_inside_double_quotes(self) -> None:
        self.assertEqual(frontmatter.loads('t: "a\\nb\\tc\\\\d"')["t"], "a\nb\tc\\d")

    def test_unterminated_quote_is_kept_verbatim(self) -> None:
        self.assertEqual(frontmatter.loads('t: "unclosed')["t"], '"unclosed')

    def test_mismatched_quote_pair_is_kept_verbatim(self) -> None:
        self.assertEqual(frontmatter.loads("t: \"mixed'")["t"], "\"mixed'")

    def test_colon_in_an_unquoted_value_splits_on_the_first_colon_only(self) -> None:
        self.assertEqual(frontmatter.loads("t: a: b: c")["t"], "a: b: c")


class TestComments(unittest.TestCase):
    def test_hash_after_whitespace_starts_a_comment(self) -> None:
        self.assertEqual(frontmatter.loads("t: value # note")["t"], "value")

    def test_whole_line_comments_are_dropped(self) -> None:
        parsed = frontmatter.loads("# top\nt: value\n  # indented\n")
        self.assertEqual(parsed, {"t": "value"})

    def test_hash_inside_a_word_is_not_a_comment(self) -> None:
        for raw, expected in (
            ("t: C# generics", "C# generics"),
            ("t: issue#12", "issue#12"),
            ("t: https://example.com/a#frag", "https://example.com/a#frag"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(frontmatter.loads(raw)["t"], expected)

    def test_comment_after_a_quoted_value_is_stripped(self) -> None:
        self.assertEqual(frontmatter.loads('t: "a # b"  # note')["t"], "a # b")

    def test_comment_after_a_list_item(self) -> None:
        parsed = frontmatter.loads("sources:\n  - https://x/y#frag  # keep\n")
        self.assertEqual(parsed["sources"], ["https://x/y#frag"])


class TestLists(unittest.TestCase):
    def test_inline_list(self) -> None:
        self.assertEqual(frontmatter.loads("tags: [a, b]")["tags"], ["a", "b"])

    def test_block_list(self) -> None:
        self.assertEqual(frontmatter.loads("tags:\n  - a\n  - b\n")["tags"], ["a", "b"])

    def test_block_list_at_the_key_column(self) -> None:
        self.assertEqual(frontmatter.loads("tags:\n- a\n- b\n")["tags"], ["a", "b"])

    def test_inline_and_block_agree(self) -> None:
        self.assertEqual(
            frontmatter.loads("tags: [a, b]"), frontmatter.loads("tags:\n  - a\n  - b\n")
        )

    def test_quoted_comma_does_not_split_an_item(self) -> None:
        """knowledge.md §3 12: the old splitter turned this into three items."""
        self.assertEqual(frontmatter.loads('t: [a, "b, c"]')["t"], ["a", "b, c"])

    def test_apostrophe_does_not_swallow_the_rest_of_the_list(self) -> None:
        """The shape scribe/default_templates/handoff.md ships."""
        self.assertEqual(
            frontmatter.loads(
                "required: [What was done, Key decisions, What's pending, Where it stopped]"
            )["required"],
            ["What was done", "Key decisions", "What's pending", "Where it stopped"],
        )

    def test_nested_brackets_do_not_close_the_list(self) -> None:
        """A glob character class — a real ``areas:`` value in the live store."""
        self.assertEqual(
            frontmatter.loads("areas: [ideas/*/0[0-9]-*.md, GATES.md]")["areas"],
            ["ideas/*/0[0-9]-*.md", "GATES.md"],
        )

    def test_empty_and_bare_forms(self) -> None:
        self.assertEqual(frontmatter.loads("tags: []")["tags"], [])
        self.assertEqual(frontmatter.loads("tags: [ ]")["tags"], [])
        self.assertEqual(frontmatter.loads("tags:")["tags"], [])
        self.assertEqual(frontmatter.loads("tags:\nother: x")["tags"], [])

    def test_quoted_brackets_stay_a_string(self) -> None:
        self.assertEqual(frontmatter.loads('note: "[]"')["note"], "[]")

    def test_unbalanced_bracket_is_a_scalar(self) -> None:
        self.assertEqual(frontmatter.loads("t: [unclosed")["t"], "[unclosed")


class TestNesting(unittest.TestCase):
    def test_one_level_of_nesting(self) -> None:
        text = "name: Model selection\nmetadata:\n  type: feedback\n  version: \"1.0\"\n"
        self.assertEqual(
            frontmatter.loads(text),
            {"name": "Model selection", "metadata": {"type": "feedback", "version": "1.0"}},
        )

    def test_list_inside_a_nested_map(self) -> None:
        text = "metadata:\n  tags: [a, b]\n  sources:\n    - x\n    - y\n"
        self.assertEqual(
            frontmatter.loads(text),
            {"metadata": {"tags": ["a", "b"], "sources": ["x", "y"]}},
        )

    def test_nesting_ends_at_the_next_top_level_key(self) -> None:
        text = "metadata:\n  type: feedback\nname: after\n"
        self.assertEqual(
            frontmatter.loads(text), {"metadata": {"type": "feedback"}, "name": "after"}
        )

    def test_empty_map_is_representable(self) -> None:
        self.assertEqual(frontmatter.loads("metadata: {}")["metadata"], {})

    def test_quoted_braces_stay_a_string(self) -> None:
        self.assertEqual(frontmatter.loads('t: "{}"')["t"], "{}")


class TestErrors(unittest.TestCase):
    def test_line_without_a_colon(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.loads("no colon here\n")

    def test_list_item_without_a_parent_key(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.loads("- orphan\n")

    def test_indented_first_line(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.loads("  t: value\n")

    def test_empty_key(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.loads(": value\n")

    def test_error_is_a_value_error(self) -> None:
        """Callers already catching ValueError keep working."""
        self.assertTrue(issubclass(frontmatter.FrontmatterError, ValueError))

    def test_error_carries_the_line_number(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError) as ctx:
            frontmatter.loads("a: 1\nno colon\n")
        self.assertEqual(ctx.exception.line, 2)


class TestParseAndDump(unittest.TestCase):
    def test_body_bytes_are_preserved(self) -> None:
        text = "---\nt: x\n---\nline one\r\n\n  indented\n\n"
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta, {"t": "x"})
        self.assertEqual(body, "line one\r\n\n  indented\n\n")

    def test_no_frontmatter_returns_the_whole_text_as_body(self) -> None:
        self.assertEqual(frontmatter.parse("just a body\n"), ({}, "just a body\n"))

    def test_unterminated_fence_is_tolerated(self) -> None:
        text = "---\nt: x\nnever closed\n"
        self.assertEqual(frontmatter.parse(text), ({}, text))

    def test_malformed_block_is_tolerated(self) -> None:
        text = "---\nno colon here\n---\nbody\n"
        self.assertEqual(frontmatter.parse(text), ({}, text))

    def test_strict_raises_where_tolerant_shrugs(self) -> None:
        for text in ("just a body\n", "---\nt: x\nnever closed\n", "---\nbad\n---\nb\n"):
            with self.subTest(text=text):
                with self.assertRaises(frontmatter.FrontmatterError):
                    frontmatter.parse(text, strict=True)

    def test_empty_meta_emits_no_fences(self) -> None:
        self.assertEqual(frontmatter.dump({}, "plain body\n"), "plain body\n")

    def test_body_opening_with_a_rule_still_gets_fences(self) -> None:
        """knowledge.md §3 12: a body starting with ``---`` was swallowed."""
        body = "---\na horizontal rule\n---\nmore\n"
        text = frontmatter.dump({}, body)
        self.assertEqual(frontmatter.parse(text), ({}, body))

    def test_dump_rejects_an_unknown_list_style(self) -> None:
        with self.assertRaises(ValueError):
            frontmatter.dumps({"a": ["b"]}, list_style="flow")


class TestRoundTrip(unittest.TestCase):
    """``parse(dump(x)) == x`` for everything the dialect can represent."""

    def _round_trip_scalar(self, value: str, style: str) -> str:
        text = frontmatter.dumps({"t": value}, list_style=style)
        return frontmatter.loads(text)["t"]

    def test_scalars(self) -> None:
        for style in ("block", "inline"):
            for value in HOSTILE_VALUES:
                with self.subTest(style=style, value=value):
                    self.assertEqual(self._round_trip_scalar(value, style), value)

    def test_list_items(self) -> None:
        for style in ("block", "inline"):
            for value in HOSTILE_VALUES:
                with self.subTest(style=style, value=value):
                    data = {"sources": [value]}
                    text = frontmatter.dumps(data, list_style=style)
                    self.assertEqual(frontmatter.loads(text), data)

    def test_repeated_round_trips_are_stable(self) -> None:
        """``/research verify`` re-reads and re-writes; three passes must not compound."""
        for style in ("block", "inline"):
            for value in HOSTILE_VALUES:
                with self.subTest(style=style, value=value):
                    current = value
                    for _ in range(3):
                        current = self._round_trip_scalar(current, style)
                    self.assertEqual(current, value)

    def test_whole_document(self) -> None:
        meta = {
            "title": "Claude Code GUI: interactive-wrapper vs Agent-SDK billing",
            "tags": ["multiplayer", "c#", "b, c"],
            "empty_list": [],
            "empty_map": {},
            "areas": ["ideas/*/0[0-9]-*.md", "**/*.py"],
            "metadata": {"type": "reference", "version": "1.0", "aka": ["x", "y"]},
            "sources": ["https://example.com/a#frag", "http://h:8080/p"],
        }
        body = "# Heading\n\nSome body with --- inside it.\n"
        for style in ("block", "inline"):
            with self.subTest(style=style):
                text = frontmatter.dump(meta, body, list_style=style)
                self.assertEqual(frontmatter.parse(text), (meta, body))

    def test_dumping_is_idempotent(self) -> None:
        meta = {"t": "Foo: bar", "tags": ["a", "b, c"]}
        first = frontmatter.dumps(meta)
        self.assertEqual(frontmatter.dumps(frontmatter.loads(first)), first)


class TestSplit(unittest.TestCase):
    def test_returns_none_without_a_complete_block(self) -> None:
        self.assertIsNone(frontmatter.split("no fence\n"))
        self.assertIsNone(frontmatter.split("---\nt: x\n"))

    def test_trailing_whitespace_on_a_fence_is_tolerated(self) -> None:
        self.assertEqual(frontmatter.split("--- \nt: x\n---  \nbody"), ("t: x\n", "body"))

    def test_indented_rule_is_not_a_fence(self) -> None:
        self.assertIsNone(frontmatter.split("---\nt: x\n  ---\nbody"))


if __name__ == "__main__":
    unittest.main()
