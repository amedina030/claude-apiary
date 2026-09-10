"""Tests for prose/markdown.py: block classification, inline stripping, scopes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import markdown as md

HANDOFF_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "scribe" / "default_templates" / "handoff.md"
)


def kinds(text: str) -> list[str]:
    return [b.kind for b in md.blocks(text)]


class BlockClassificationTest(unittest.TestCase):
    def test_paragraphs_and_headings(self):
        text = "# Title\n\nFirst para line one\nline two.\n\n## Sub\n\nSecond para."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["heading", "paragraph", "heading", "paragraph"])
        self.assertEqual(got[0].prose, "Title")
        self.assertEqual((got[1].start_line, got[1].end_line), (3, 4))
        self.assertEqual(got[1].prose, "First para line one\nline two.")

    def test_setext_heading(self):
        text = "Title here\n=====\n\nBody.\n\nSecond\n---\n\nMore."
        self.assertEqual(kinds(text), ["heading", "paragraph", "heading", "paragraph"])
        self.assertEqual(md.blocks(text)[0].prose, "Title here\n")

    def test_thematic_break_is_not_a_setext_underline(self):
        text = "Para.\n\n---\n\nNext."
        self.assertEqual(kinds(text), ["paragraph", "thematic", "paragraph"])

    def test_front_matter(self):
        text = "---\ntitle: x\ntags: [a, b]\n---\n\nBody; with a semicolon."
        got = md.blocks(text)
        self.assertEqual(got[0].kind, "frontmatter")
        self.assertEqual((got[0].start_line, got[0].end_line), (1, 4))
        self.assertEqual(got[1].kind, "paragraph")
        self.assertEqual(got[1].start_line, 6)

    def test_fenced_code_with_a_list_inside(self):
        text = "Intro.\n\n```bash\n- not a list; code\n```\n\nAfter."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["paragraph", "code", "paragraph"])
        self.assertEqual((got[1].start_line, got[1].end_line), (3, 5))
        self.assertEqual(got[1].prose, "")

    def test_unterminated_fence_runs_to_eof(self):
        text = "Intro.\n\n~~~\ncode\nmore code"
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["paragraph", "code"])
        self.assertEqual(got[1].end_line, 5)

    def test_indented_code_outside_a_list(self):
        text = "Run it:\n\n    python x.py --flag; echo done\n\nThen read."
        self.assertEqual(kinds(text), ["paragraph", "code", "paragraph"])

    def test_list_items_are_separate_blocks_with_continuations(self):
        text = "- first item\n  continues here\n- second item\n\n- third after blank\n\n  indented follow-up of third"
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["list", "list", "list", "list"])
        self.assertEqual(got[0].prose, "first item\ncontinues here")
        self.assertEqual(got[1].prose, "second item")
        self.assertEqual(got[3].prose, "indented follow-up of third")

    def test_nested_and_numbered_lists(self):
        text = "1. one\n   - nested a\n   - nested b\n2) two\n* star\n+ plus"
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["list"] * 6)
        self.assertEqual(
            [b.prose for b in got], ["one", "nested a", "nested b", "two", "star", "plus"]
        )

    def test_task_list_checkbox_is_stripped(self):
        got = md.blocks("- [ ] todo item\n- [x] done item")
        self.assertEqual([b.prose for b in got], ["todo item", "done item"])

    def test_list_marker_interrupts_a_paragraph(self):
        text = "Lead sentence.\n- item"
        self.assertEqual(kinds(text), ["paragraph", "list"])

    def test_flush_left_line_after_list_is_a_paragraph(self):
        text = "- item\n\nBack to prose."
        self.assertEqual(kinds(text), ["list", "paragraph"])

    def test_blockquote(self):
        text = "> quoted line\n> second\nlazy continuation\n\nAfter."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["blockquote", "paragraph"])
        self.assertEqual(got[0].prose, "quoted line\nsecond\nlazy continuation")

    def test_table_with_delimiter_row(self):
        text = "| Tool | Purpose |\n|---|---|\n| `x.py` | Does a thing — with a dash |\n\nAfter."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["table", "paragraph"])
        self.assertEqual((got[0].start_line, got[0].end_line), (1, 3))
        self.assertIn("Does a thing — with a dash", got[0].prose)
        self.assertNotIn("|", got[0].prose)

    def test_table_rows_without_delimiter(self):
        text = "| a | b |\n| c | d |"
        self.assertEqual(kinds(text), ["table"])

    def test_html_comment_and_block(self):
        text = "<!-- generated:start: x -->\n| a |\n|---|\n<!-- generated:end: x -->\n\n<div>\nraw html\n</div>\n\nProse."
        self.assertEqual(kinds(text), ["html", "table", "html", "html", "paragraph"])

    def test_multiline_html_comment(self):
        text = "<!--\nhidden; text\n-->\n\nShown."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["html", "paragraph"])
        self.assertEqual((got[0].start_line, got[0].end_line), (1, 3))

    def test_prose_off_on_directives_mute_blocks(self):
        text = "Scored.\n\n<!-- prose off -->\n\nMuted; text.\n\n- muted item\n\n<!-- prose on -->\n\nScored again."
        got = md.blocks(text)
        self.assertEqual(
            [b.kind for b in got], ["paragraph", "html", "paragraph", "list", "html", "paragraph"]
        )
        self.assertEqual(
            [b.prose for b in got if b.kind != "html"], ["Scored.", "", "", "Scored again."]
        )
        self.assertEqual([s.text for s in md.segments(text, "text")], ["Scored.", "Scored again."])
        self.assertIn("Muted; text.", md.segments(text, "raw")[0].text)

    def test_link_reference_definition(self):
        text = "[ref]: https://example.invalid/x\n\nText [with][ref] a ref link."
        got = md.blocks(text)
        self.assertEqual([b.kind for b in got], ["linkdef", "paragraph"])
        self.assertEqual(got[1].prose, "Text with a ref link.")

    def test_handoff_template_role_line_is_a_paragraph(self):
        text = HANDOFF_TEMPLATE.read_text(encoding="utf-8")
        got = md.blocks(text)
        role = next(b for b in got if b.raw.startswith("**Role:**"))
        self.assertEqual(role.kind, "paragraph")
        self.assertEqual(role.prose, "Role: <role> | Mission: <mission>")
        self.assertEqual(got[0].kind, "frontmatter")
        self.assertTrue(any(b.kind == "list" for b in got))


class InlineStrippingTest(unittest.TestCase):
    def test_code_spans_are_removed(self):
        self.assertEqual(md.strip_inline("run `a; b — c` now"), "run   now")
        self.assertEqual(md.strip_inline("double ``x`y`` end"), "double   end")

    def test_links_keep_text(self):
        self.assertEqual(
            md.strip_inline("see [the doc](https://x.invalid/a) and ![img](p.png)"),
            "see the doc and img",
        )
        self.assertEqual(md.strip_inline("bare https://x.invalid/path;q end"), "bare   end")
        self.assertEqual(md.strip_inline("auto <https://x.invalid> end"), "auto   end")

    def test_emphasis_markers_are_removed_but_identifiers_keep_underscores(self):
        self.assertEqual(
            md.strip_inline("**bold** and *it* and __u__ and _i_"), "bold and it and u and i"
        )
        self.assertEqual(md.strip_inline("call snake_case_name here"), "call snake_case_name here")
        self.assertEqual(md.strip_inline("~~gone~~ text"), "gone text")

    def test_inline_html_and_escapes(self):
        self.assertEqual(md.strip_inline("a<br>b <kbd>Ctrl</kbd> \\- dash"), "a b  Ctrl  - dash")

    def test_line_count_is_preserved(self):
        text = "one `code`\ntwo [l](u)\nthree"
        self.assertEqual(md.strip_inline(text).count("\n"), 2)


class ScopesTest(unittest.TestCase):
    TEXT = (
        "# Heading — with dash\n\n"
        "Paragraph one; has a semicolon. Second sentence here!\nStill paragraph one.\n\n"
        "- item one; semicolon\n- item two\n\n"
        "> quote\n\n"
        "| a | b |\n|---|---|\n| c | d |\n\n"
        "```\ncode; here\n```\n"
    )

    def test_paragraph_scope_excludes_lists_headings_tables_quotes_code(self):
        segs = md.segments(self.TEXT, "paragraph")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].line, 3)
        self.assertNotIn("item", segs[0].text)

    def test_list_scope(self):
        segs = md.segments(self.TEXT, "list")
        self.assertEqual([s.text for s in segs], ["item one; semicolon", "item two"])
        self.assertEqual([s.line for s in segs], [6, 7])

    def test_text_scope_covers_all_prose_kinds_but_not_code(self):
        segs = md.segments(self.TEXT, "text")
        self.assertEqual(
            [s.kind for s in segs], ["heading", "paragraph", "list", "list", "blockquote", "table"]
        )
        self.assertFalse(any("code" in s.text for s in segs))

    def test_raw_scope_is_the_whole_document(self):
        segs = md.segments(self.TEXT, "raw")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].text, self.TEXT)

    def test_sentence_scope_splits_and_keeps_lines(self):
        segs = [s for s in md.segments(self.TEXT, "sentence") if s.kind == "paragraph"]
        self.assertEqual(
            [s.text.strip() for s in segs],
            ["Paragraph one; has a semicolon.", "Second sentence here!", "Still paragraph one."],
        )
        self.assertEqual([s.line for s in segs], [3, 3, 4])

    def test_unknown_scope_raises(self):
        with self.assertRaises(ValueError):
            md.segments("x", "chapter")

    def test_line_of_maps_offsets_across_newlines(self):
        seg = md.Segment(10, "a\nb\nc")
        self.assertEqual(seg.line_of(0), 10)
        self.assertEqual(seg.line_of(2), 11)
        self.assertEqual(seg.line_of(4), 12)


if __name__ == "__main__":
    unittest.main()
