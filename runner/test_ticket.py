#!/usr/bin/env python3
"""Tests for runner/ticket.py — the consolidated ticket-lifecycle CLI.

Carries over the coverage the five separate scripts had (handoff parsing,
slugs, hint parsing, intake/backlog writing) and adds the parts that only
exist now: the subcommand wiring, and parity between each deprecated shim
entry point and the subcommand it forwards to.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import ticket

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        sections = ticket.parse_sections(SAMPLE_HANDOFF)
        for name in ("Goal", "Shape", "Behavior", "Boundaries", "Acceptance criteria"):
            self.assertIn(name, sections)

    def test_section_body_content(self):
        sections = ticket.parse_sections(SAMPLE_HANDOFF)
        self.assertIn("Foo currently does X", sections["Goal"])
        self.assertIn("existing pipeline pattern", sections["Shape"])

    def test_empty_input(self):
        self.assertEqual(ticket.parse_sections(""), {})

    def test_prose_before_first_header_is_discarded(self):
        text = "random preamble\n\n## Goal\n- **Problem:** P.\n"
        self.assertEqual(list(ticket.parse_sections(text).keys()), ["Goal"])


class TestExtractField(unittest.TestCase):
    def test_simple_field(self):
        body = "- **Problem:** A pain point.\n- **Solution:** A fix."
        self.assertEqual(ticket.extract_field(body, "Problem"), "A pain point.")
        self.assertEqual(ticket.extract_field(body, "Solution"), "A fix.")

    def test_missing_field_returns_none(self):
        self.assertIsNone(ticket.extract_field("nothing here", "Problem"))

    def test_multiline_field(self):
        body = "- **Problem:** Line one\n  continues on line two.\n- **Solution:** S."
        got = ticket.extract_field(body, "Problem")
        self.assertIn("Line one", got)
        self.assertIn("continues on line two", got)


class TestMapToIntake(unittest.TestCase):
    def test_happy_path(self):
        mapped = ticket.map_to_intake(ticket.parse_sections(SAMPLE_HANDOFF))
        self.assertEqual(mapped["problem"], "Foo currently does X which is painful.")
        self.assertIn("SHAPE:", mapped["description"])
        self.assertIn("BEHAVIOR:", mapped["description"])
        self.assertIn("Alpha", mapped["description"])
        self.assertIn("In scope", mapped["scope"])
        self.assertIn("ACCEPTANCE CRITERIA:", mapped["context"])

    def test_missing_section_raises(self):
        with self.assertRaises(ValueError) as cm:
            ticket.map_to_intake({"Goal": "- **Problem:** x." * 10})
        self.assertIn("Shape", str(cm.exception))

    def test_missing_problem_raises(self):
        sections = ticket.parse_sections(SAMPLE_HANDOFF)
        sections["Goal"] = "- **Solution:** just a fix."
        with self.assertRaises(ValueError) as cm:
            ticket.map_to_intake(sections)
        self.assertIn("Problem", str(cm.exception))


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(ticket.ticket_slug("Hello World"), "hello-world")

    def test_specials(self):
        self.assertEqual(ticket.ticket_slug("A: B/C?!"), "a-b-c")

    def test_truncation(self):
        self.assertLessEqual(len(ticket.ticket_slug("x" * 200)), 60)

    def test_empty_title_gives_empty_slug_not_a_default_name(self):
        self.assertEqual(ticket.ticket_slug("!!!"), "")

    def test_shares_the_branch_slugifier(self):
        """One slugify in the package (review X-3): the run branch uses the
        same function uncapped, with an 'item' fallback."""
        from runner.detached_lib import slugify

        self.assertEqual(slugify("Hello World!"), "hello-world")
        self.assertEqual(slugify(""), "item")
        self.assertEqual(slugify("", max_length=60, fallback=""), "")


class TestParseHints(unittest.TestCase):
    def test_dedupe_and_strip(self):
        self.assertEqual(
            ticket.parse_hints(" a.py , b.py, a.py ,, c.py"),
            ["a.py", "b.py", "c.py"],
        )

    def test_empty(self):
        self.assertEqual(ticket.parse_hints(""), [])


class TicketDirsMixin:
    """Point the module's intake/backlog dirs at a tempdir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.intake = self.root / "intake"
        self.backlog = self.root / "backlog"
        for patcher in (
            mock.patch.object(ticket, "INTAKE_DIR", self.intake),
            mock.patch.object(ticket, "BACKLOG_DIR", self.backlog),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, *argv) -> int:
        parser = ticket.build_parser()
        args = parser.parse_args(list(argv))
        return args.handler(args, parser)


_LONG = "A long enough field to clear the twenty character minimum."


class TestDraftAndPromote(TicketDirsMixin, unittest.TestCase):
    def _draft(self, title="Add caching"):
        return self._run(
            "draft",
            "--title",
            title,
            "--problem",
            _LONG,
            "--description",
            _LONG,
            "--scope",
            "api/cache.py",
        )

    def test_draft_writes_a_slugged_backlog_file(self):
        self.assertEqual(self._draft(), 0)
        self.assertTrue((self.backlog / "add-caching.json").exists())

    def test_draft_refuses_to_overwrite(self):
        self.assertEqual(self._draft(), 0)
        self.assertEqual(self._draft(), 1)

    def test_draft_rejects_a_title_that_slugs_to_nothing(self):
        rc = self._run(
            "draft",
            "--title",
            "???",
            "--problem",
            _LONG,
            "--description",
            _LONG,
            "--scope",
            "x",
        )
        self.assertEqual(rc, 1)

    def test_promote_moves_the_ticket_and_validates_it(self):
        self.assertEqual(self._draft(), 0)
        self.assertEqual(self._run("promote", "add-caching"), 0)
        self.assertFalse((self.backlog / "add-caching.json").exists())
        written = list(self.intake.glob("*.json"))
        self.assertEqual(len(written), 1)
        data = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Add caching")

    def test_promote_rejects_a_path_separator_slug(self):
        self.assertEqual(self._run("promote", "foo/bar"), 1)

    def test_promote_reports_a_missing_ticket(self):
        self.assertEqual(self._run("promote", "nope"), 1)

    def test_promote_carries_target_repo_through(self):
        self.assertEqual(self._draft(), 0)
        path = self.backlog / "add-caching.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["target_repo"] = str(REPO_ROOT)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self._run("promote", "add-caching"), 0)
        written = json.loads(next(self.intake.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(written["target_repo"], str(REPO_ROOT))


class TestMarkDone(TicketDirsMixin, unittest.TestCase):
    def _draft(self, title="Add caching"):
        return self._run(
            "draft",
            "--title",
            title,
            "--problem",
            _LONG,
            "--description",
            _LONG,
            "--scope",
            "api/cache.py",
        )

    def test_deletes_the_backlog_file(self):
        self.assertEqual(self._draft(), 0)
        self.assertEqual(self._run("mark-done", "add-caching"), 0)
        self.assertFalse((self.backlog / "add-caching.json").exists())

    def test_note_is_accepted_and_ignored(self):
        self.assertEqual(self._draft(), 0)
        self.assertEqual(self._run("mark-done", "add-caching", "--note", "fixed by hand"), 0)
        self.assertFalse((self.backlog / "add-caching.json").exists())

    def test_rejects_a_path_separator_slug(self):
        self.assertEqual(self._run("mark-done", "foo/bar"), 1)
        self.assertEqual(self._run("mark-done", "..\\escape"), 1)

    def test_reports_a_missing_ticket(self):
        self.assertEqual(self._run("mark-done", "nope"), 1)

    def test_leaves_intake_alone(self):
        """A promoted ticket has no backlog file, so mark-done cannot touch it."""
        self.assertEqual(self._draft(), 0)
        self.assertEqual(self._run("promote", "add-caching"), 0)
        self.assertEqual(self._run("mark-done", "add-caching"), 1)
        self.assertEqual(len(list(self.intake.glob("*.json"))), 1)


class TestCreateIntake(TicketDirsMixin, unittest.TestCase):
    def test_writes_and_validates(self):
        rc = self._run(
            "create-intake",
            "--title",
            "Add caching",
            "--problem",
            _LONG,
            "--description",
            _LONG,
            "--scope",
            "api/cache.py",
            "--explore-hints",
            "a.py, b.py, a.py",
        )
        self.assertEqual(rc, 0)
        data = json.loads(next(self.intake.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(data["explore_hints"], ["a.py", "b.py"])

    def test_deletes_the_file_when_validation_fails(self):
        # "short" is under validate_intake's 20-character minimum.
        rc = self._run(
            "create-intake",
            "--title",
            "Add caching",
            "--problem",
            "short",
            "--description",
            _LONG,
            "--scope",
            "api/cache.py",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(list(self.intake.glob("*.json")), [])

    def test_missing_fields_exit_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("create-intake", "--title", "Add caching")
        self.assertNotEqual(ctx.exception.code, 0)


class TestFromNote(TicketDirsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ticket, "read_note", return_value=SAMPLE_HANDOFF)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_intake_file(self):
        self.assertEqual(self._run("from-note", "--note", "42", "--title", "Test Title"), 0)
        written = list(self.intake.glob("*.json"))
        self.assertEqual(len(written), 1)
        data = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Test Title")
        self.assertEqual(data["source"], "scribe-note:42")
        self.assertIn("Foo currently", data["problem"])

    def test_write_backlog_file(self):
        self.assertEqual(
            self._run("from-note", "--note", "42", "--title", "Test Title", "--backlog"), 0
        )
        self.assertTrue((self.backlog / "test-title.json").exists())

    def test_malformed_handoff_is_reported(self):
        with mock.patch.object(ticket, "read_note", return_value="## Goal\nnothing"):
            rc = self._run("from-note", "--note", "42", "--title", "T")
        self.assertEqual(rc, 1)


class TestValidateSubcommand(TicketDirsMixin, unittest.TestCase):
    def test_valid_file(self):
        self.assertEqual(
            self._run(
                "create-intake",
                "--title",
                "Add caching",
                "--problem",
                _LONG,
                "--description",
                _LONG,
                "--scope",
                "api/cache.py",
            ),
            0,
        )
        path = next(self.intake.glob("*.json"))
        self.assertEqual(self._run("validate", str(path)), 0)

    def test_missing_file(self):
        self.assertEqual(self._run("validate", str(self.root / "nope.json")), 1)

    def test_invalid_file(self):
        path = self.root / "bad.json"
        path.write_text(json.dumps({"id": "x"}), encoding="utf-8")
        self.assertEqual(self._run("validate", str(path)), 1)


class TestShimParity(unittest.TestCase):
    """The five deprecated entry points still parse the same flags.

    They are kept for one release; `check_cli_claims` reconciles their
    documented flags against these parsers, so a drift here is a doc failure
    too.
    """

    def _help(self, module: str) -> str:
        res = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_create_intake_shim_flags(self):
        text = self._help("runner.create_intake")
        for flag in (
            "--title",
            "--problem",
            "--description",
            "--scope",
            "--context",
            "--from-todo",
            "--explore-hints",
        ):
            self.assertIn(flag, text)

    def test_draft_ticket_shim_flags(self):
        text = self._help("runner.draft_ticket")
        for flag in (
            "--title",
            "--problem",
            "--description",
            "--scope",
            "--context",
            "--from-todo",
        ):
            self.assertIn(flag, text)
        self.assertNotIn("--explore-hints", text)

    def test_promote_shim_takes_a_slug(self):
        self.assertIn("slug", self._help("runner.promote"))

    def test_refine_to_intake_shim_flags(self):
        text = self._help("runner.refine_to_intake")
        for flag in ("--note", "--title", "--backlog", "--explore-hints"):
            self.assertIn(flag, text)

    def test_mark_done_shim_flags(self):
        text = self._help("runner.mark_done")
        self.assertIn("slug", text)
        self.assertIn("--note", text)

    def test_shims_forward_to_the_ticket_handlers(self):
        from runner import create_intake, draft_ticket, mark_done, promote, refine_to_intake

        self.assertIs(create_intake.cmd_create_intake, ticket.cmd_create_intake)
        self.assertIs(draft_ticket.cmd_draft, ticket.cmd_draft)
        self.assertIs(promote.cmd_promote, ticket.cmd_promote)
        self.assertIs(refine_to_intake.cmd_from_note, ticket.cmd_from_note)
        self.assertIs(mark_done.cmd_mark_done, ticket.cmd_mark_done)


if __name__ == "__main__":
    unittest.main()
