#!/usr/bin/env python3
"""Tests for docs/generate_cli_docs.py.

Two jobs:

1. The committed docs are in sync — this is the ``--check`` the pre-commit hook
   and CI run, executed inside the suite so a doc cannot rot between commits.
2. The reconciliation rules behave: a flag argparse no longer has is dropped, a
   flag nothing documents is appended, a curated subset survives, and the
   ``Not introspectable`` table in cli-index.md names exactly the sections the
   checker skips (the hand-written table that must be *tested* because it
   cannot be generated).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_cli_claims as cc  # noqa: E402
import docgen  # noqa: E402
import generate_cli_docs as gen  # noqa: E402


class InSyncTests(unittest.TestCase):
    """The committed cli docs match the code they document."""

    @classmethod
    def setUpClass(cls):
        cls.generators = gen.generators()

    def test_generated_blocks_are_up_to_date(self):
        for g in self.generators:
            with self.subTest(doc=g.rel()):
                current = g.path.read_text(encoding="utf-8")
                self.assertEqual(
                    g.build(current), current,
                    f"{g.rel()} is stale — run "
                    f"`python docs/generate_cli_docs.py --write`")

    def test_every_introspectable_tool_has_a_generated_block(self):
        text = gen.CLI_TOOLS.read_text(encoding="utf-8")
        for header, iface in gen.interfaces(gen.tool_headers(text)).items():
            if not (iface.subcommands or iface.flags or iface.positionals):
                continue
            with self.subTest(tool=header):
                keys = [f"cli:{header}:{k}" for k in ("sub", "flag", "arg")]
                self.assertTrue(
                    any(docgen.block_span(text, k) is not None for k in keys),
                    f"{header} has an argparse interface but no generated table")

    def test_index_lists_every_introspectable_tool(self):
        index = gen.CLI_INDEX.read_text(encoding="utf-8")
        body = docgen.block_body(index, gen.INDEX_KEY)
        tools = gen.tool_headers(gen.CLI_TOOLS.read_text(encoding="utf-8"))
        for header in gen.interfaces(tools):
            with self.subTest(tool=header):
                self.assertIn(gen._index_key(header),
                              [gen._index_key(r[0])
                               for r in docgen.first_table(body).rows])


class SkipTableTests(unittest.TestCase):
    """cli-index.md's hand-written 'Not introspectable' table is the one table
    code cannot produce, so it is tested instead."""

    def test_it_names_exactly_the_skipped_sections(self):
        text = gen.CLI_INDEX.read_text(encoding="utf-8")
        after = text.split("## Not introspectable", 1)[1]
        table = docgen.first_table(after)
        listed = {r[0].strip("` ") for r in table.rows}
        self.assertEqual(listed, set(cc.SKIP_HEADERS))

    def test_every_skipped_section_still_exists_in_the_doc(self):
        headers = {h for h, _ in cc.parse_sections(
            gen.CLI_TOOLS.read_text(encoding="utf-8"))}
        for skipped in cc.SKIP_HEADERS:
            with self.subTest(section=skipped):
                self.assertIn(skipped, headers)


FAKE_SECTION = """\
## fake/tool.py

Prose that mentions `--kept` nowhere else.

<!-- generated:start: cli:fake/tool.py:flag -->
| Flag | Required | Description |
|------|----------|-------------|
| `--kept` | yes | hand-written prose |
| `--gone` | no | removed from argparse |
<!-- generated:end: cli:fake/tool.py:flag -->
"""


class ReconciliationTests(unittest.TestCase):
    def _iface(self, flags, descs=None):
        return cc.ToolInterface(rel_path="fake/tool.py",
                                base_argv=[sys.executable, "fake/tool.py"],
                                flags=list(flags), flag_descs=dict(descs or {}))

    def test_a_flag_argparse_lost_is_dropped(self):
        out = gen.rebuild_section(FAKE_SECTION, "fake/tool.py",
                                  self._iface(["--kept"]))
        self.assertIn("--kept", out)
        self.assertNotIn("--gone", out)

    def test_hand_written_columns_survive(self):
        out = gen.rebuild_section(FAKE_SECTION, "fake/tool.py",
                                  self._iface(["--kept"]))
        self.assertIn("hand-written prose", out)
        self.assertIn("| yes |", out)

    def test_a_flag_nothing_documents_is_appended_with_its_help(self):
        out = gen.rebuild_section(
            FAKE_SECTION, "fake/tool.py",
            self._iface(["--kept", "--brand-new"], {"--brand-new": "does a thing"}))
        self.assertIn("`--brand-new`", out)
        self.assertIn("does a thing", out)

    def test_a_flag_mentioned_only_in_prose_is_not_appended(self):
        section = FAKE_SECTION.replace("Prose that mentions",
                                       "Prose that mentions `--mentioned` and")
        out = gen.rebuild_section(section, "fake/tool.py",
                                  self._iface(["--kept", "--mentioned"]))
        self.assertEqual(out.count("--mentioned"), 1)

    def test_an_ignore_marker_suppresses_the_append(self):
        section = FAKE_SECTION.replace(
            "Prose that", "<!-- cli-claims: ignore: --secret -->\nProse that")
        out = gen.rebuild_section(section, "fake/tool.py",
                                  self._iface(["--kept", "--secret"]))
        self.assertEqual(out.count("--secret"), 1)

    def test_a_section_with_no_table_gains_one(self):
        section = "## fake/tool.py\n\nJust prose.\n"
        out = gen.rebuild_section(section, "fake/tool.py",
                                  self._iface(["--new"], {"--new": "help"}))
        self.assertIn("### Flags", out)
        self.assertIn("`--new`", out)
        self.assertIn(docgen.start_marker("cli:fake/tool.py:flag"), out)

    def test_generation_is_idempotent(self):
        iface = self._iface(["--kept", "--added"], {"--added": "h"})
        once = gen.rebuild_section(FAKE_SECTION, "fake/tool.py", iface)
        twice = gen.rebuild_section(once, "fake/tool.py", iface)
        self.assertEqual(once, twice)


class InvocationTests(unittest.TestCase):
    def test_console_scripts_are_typed_by_name(self):
        iface = cc.ToolInterface(rel_path="core/cli.py",
                                 base_argv=[sys.executable, "core/cli.py"])
        self.assertEqual(gen.invocation("apiary", iface), "apiary")

    def test_module_only_tools_render_as_dash_m(self):
        iface = cc.ToolInterface(rel_path="runner/run.py",
                                 base_argv=[sys.executable, "-m", "runner.run"])
        self.assertEqual(gen.invocation("runner/run.py", iface),
                         "python -m runner.run")

    def test_index_key_normalises_every_spelling(self):
        for spelling in ("runner/run.py", "python runner/run.py",
                         "`python -m runner.run`"):
            self.assertEqual(gen._index_key(spelling), "runner/run.py")


if __name__ == "__main__":
    unittest.main()
