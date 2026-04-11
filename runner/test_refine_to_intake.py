#!/usr/bin/env python3
"""Tests for runner/refine_to_intake.py.

Stdlib unittest only.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import refine_to_intake


SAMPLE_HANDOFF = """## Goal
- **Problem:** Foo currently does X which is painful.
- **Solution:** Add Y to fix it.
- **Value:** Users get Z.

## Shape
- **Components:**
  - Alpha: parses things
  - Beta: writes things
- **Integration point:** bar.py
- **Pattern:** existing pipeline pattern
- **Data flow:** input → alpha → beta → output
- **Dependencies:** none

## Behavior
- **Input:** JSON via stdin
- **Processing:** 1. parse 2. transform 3. emit
- **Output:** JSON on stdout
- **Error cases:**
  - bad json → exit 1
- **Edge cases:**
  - empty input → empty output

## Boundaries
- **In scope:** Alpha and Beta modules
- **Out of scope:** Gamma — deferred to next sprint
- **Must not break:** existing bar.py CLI

## Acceptance criteria
- [ ] Given valid input, when run, then emits transformed JSON
- [ ] Given bad json, when run, then exits 1
"""


class TestParseSections(unittest.TestCase):
    def test_all_sections_present(self):
        sections = refine_to_intake.parse_sections(SAMPLE_HANDOFF)
        self.assertIn("Goal", sections)
        self.assertIn("Shape", sections)
        self.assertIn("Behavior", sections)
        self.assertIn("Boundaries", sections)
        self.assertIn("Acceptance criteria", sections)

    def test_section_body_content(self):
        sections = refine_to_intake.parse_sections(SAMPLE_HANDOFF)
        self.assertIn("Foo currently does X", sections["Goal"])
        self.assertIn("existing pipeline pattern", sections["Shape"])

    def test_empty_input(self):
        self.assertEqual(refine_to_intake.parse_sections(""), {})

    def test_prose_before_first_header_is_discarded(self):
        text = "random preamble\n\n## Goal\n- **Problem:** P.\n"
        sections = refine_to_intake.parse_sections(text)
        self.assertEqual(list(sections.keys()), ["Goal"])


class TestExtractField(unittest.TestCase):
    def test_simple_field(self):
        body = "- **Problem:** A pain point.\n- **Solution:** A fix."
        self.assertEqual(refine_to_intake.extract_field(body, "Problem"), "A pain point.")
        self.assertEqual(refine_to_intake.extract_field(body, "Solution"), "A fix.")

    def test_missing_field_returns_none(self):
        self.assertIsNone(refine_to_intake.extract_field("nothing here", "Problem"))

    def test_multiline_field(self):
        body = "- **Problem:** Line one\n  continues on line two.\n- **Solution:** S."
        got = refine_to_intake.extract_field(body, "Problem")
        self.assertIn("Line one", got)
        self.assertIn("continues on line two", got)


class TestMapToIntake(unittest.TestCase):
    def test_happy_path(self):
        sections = refine_to_intake.parse_sections(SAMPLE_HANDOFF)
        mapped = refine_to_intake.map_to_intake(sections)
        self.assertEqual(mapped["problem"], "Foo currently does X which is painful.")
        self.assertIn("SHAPE:", mapped["description"])
        self.assertIn("BEHAVIOR:", mapped["description"])
        self.assertIn("Alpha", mapped["description"])
        self.assertIn("In scope", mapped["scope"])
        self.assertIn("ACCEPTANCE CRITERIA:", mapped["context"])

    def test_missing_section_raises(self):
        sections = {"Goal": "- **Problem:** x." * 10}
        with self.assertRaises(ValueError) as cm:
            refine_to_intake.map_to_intake(sections)
        self.assertIn("Shape", str(cm.exception))

    def test_missing_problem_raises(self):
        sections = refine_to_intake.parse_sections(SAMPLE_HANDOFF)
        sections["Goal"] = "- **Solution:** just a fix."
        with self.assertRaises(ValueError) as cm:
            refine_to_intake.map_to_intake(sections)
        self.assertIn("Problem", str(cm.exception))


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(refine_to_intake.slugify("Hello World"), "hello-world")

    def test_specials(self):
        self.assertEqual(refine_to_intake.slugify("A: B/C?!"), "a-b-c")

    def test_truncation(self):
        self.assertLessEqual(len(refine_to_intake.slugify("x" * 200)), 60)


class TestParseHints(unittest.TestCase):
    def test_dedupe_and_strip(self):
        self.assertEqual(
            refine_to_intake.parse_hints(" a.py , b.py, a.py ,, c.py"),
            ["a.py", "b.py", "c.py"],
        )

    def test_empty(self):
        self.assertEqual(refine_to_intake.parse_hints(""), [])


class TestMainIntegration(unittest.TestCase):
    """Exercise main() via monkeypatching read_note and the intake dir."""

    def test_write_intake_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch.object(refine_to_intake, "read_note", return_value=SAMPLE_HANDOFF), \
                 mock.patch.object(refine_to_intake, "INTAKE_DIR", tmp_path), \
                 mock.patch.object(sys, "argv", [
                     "refine_to_intake.py", "--note", "42", "--title", "Test Title"
                 ]):
                refine_to_intake.main()
            files = list(tmp_path.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["title"], "Test Title")
            self.assertEqual(data["source"], "scribe-note:42")
            self.assertIn("Foo currently", data["problem"])

    def test_write_backlog_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch.object(refine_to_intake, "read_note", return_value=SAMPLE_HANDOFF), \
                 mock.patch.object(refine_to_intake, "BACKLOG_DIR", tmp_path), \
                 mock.patch.object(sys, "argv", [
                     "refine_to_intake.py", "--note", "42", "--title", "Test Title", "--backlog"
                 ]):
                refine_to_intake.main()
            self.assertTrue((tmp_path / "test-title.json").exists())


if __name__ == "__main__":
    unittest.main()
