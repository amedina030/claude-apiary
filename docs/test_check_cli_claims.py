#!/usr/bin/env python3
"""Tests for docs/check_cli_claims.py — the CLI-claim reconciliation checker.

Uses a throwaway argparse tool written into a tempdir so introspection runs
against a known parser, and asserts drift is detected in both directions.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_cli_claims as cc


# A minimal argparse CLI with two subcommands and a couple of flags, used as the
# "real" tool that doc claims are reconciled against.
FAKE_TOOL = '''\
import argparse
def main():
    p = argparse.ArgumentParser(description="fake")
    p.add_argument("--project")
    sub = p.add_subparsers(dest="command")
    a = sub.add_parser("add")
    a.add_argument("--type")
    a.add_argument("--content")
    sub.add_parser("list")
    p.parse_args()
if __name__ == "__main__":
    main()
'''


class DocParsingTests(unittest.TestCase):
    def test_parse_sections_splits_on_top_level_headers_only(self):
        text = "## a/x.py\nbody1\n### Subcommands\nrow\n## b/y.py\nbody2\n"
        sections = dict(cc.parse_sections(text))
        self.assertEqual(set(sections), {"a/x.py", "b/y.py"})
        self.assertIn("### Subcommands", sections["a/x.py"])

    def test_doc_subcommands_reads_first_column_and_skips_none(self):
        section = (
            "### Subcommands\n"
            "| Subcommand | Description |\n"
            "|------------|-------------|\n"
            "| `add` | adds |\n"
            "| `list` | lists |\n"
            "| (none) | default |\n"
        )
        self.assertEqual(cc.doc_subcommands(section), {"add", "list"})

    def test_doc_flags_reads_flag_table_and_ignores_prose_and_examples(self):
        section = (
            "| Flag | Description |\n"
            "|------|-------------|\n"
            "| `--type TYPE` | a type |\n"
            "| `--content TEXT` | content |\n"
            "\n"
            "Run `git rev-parse --show-toplevel` first. See `report.py --by-request`.\n"
        )
        # only the flag-table first column counts — not the bash example or x-ref
        self.assertEqual(cc.doc_flags(section), {"--type", "--content"})

    def test_doc_flags_handles_argument_slash_flag_header(self):
        section = (
            "| Argument / Flag | Required | Description |\n"
            "|-----------------|----------|-------------|\n"
            "| `intake_path` | yes | positional |\n"
            "| `--target-repo PATH` | no | a flag |\n"
        )
        self.assertEqual(cc.doc_flags(section), {"--target-repo"})

    def test_doc_ignores_parses_marker(self):
        section = "text <!-- cli-claims: ignore: --legacy, oldsub --> more"
        self.assertEqual(cc.doc_ignores(section), {"--legacy", "oldsub"})


class HelpParsingTests(unittest.TestCase):
    def test_help_subcommands_reads_positional_brace_group(self):
        help_text = (
            "usage: x [-h] {add,list} ...\n\n"
            "positional arguments:\n"
            "  {add,list}\n\n"
            "options:\n"
            "  -h, --help\n"
        )
        self.assertEqual(cc.help_subcommands(help_text), {"add", "list"})

    def test_help_subcommands_ignores_flag_choices(self):
        # A flag's {todo,handoff} choices live under options:, not positional args.
        help_text = (
            "usage: x [-h] [--type {todo,handoff}]\n\n"
            "options:\n"
            "  -h, --help\n"
            "  --type {todo,handoff}\n"
        )
        self.assertEqual(cc.help_subcommands(help_text), set())

    def test_help_flags_extracts_long_flags(self):
        help_text = "options:\n  -h, --help\n  --project PROJECT\n  --type TYPE\n"
        self.assertEqual(cc.help_flags(help_text), {"--project", "--type"})


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        tool_dir = self.root / "tool"
        tool_dir.mkdir()
        (tool_dir / "fake.py").write_text(FAKE_TOOL, encoding="utf-8")
        # Point the checker's repo root at the tempdir so it introspects our fake.
        self._orig_root = cc.REPO_ROOT
        cc.REPO_ROOT = self.root

    def tearDown(self):
        cc.REPO_ROOT = self._orig_root
        self._tmp.cleanup()

    SUBS = (
        "### Subcommands\n"
        "| Subcommand | Description |\n"
        "|------------|-------------|\n"
        "| `add` | adds |\n"
        "| `list` | lists |\n"
    )
    FLAGS_FULL = (
        "\n### Flags\n"
        "| Flag | Description |\n"
        "|------|-------------|\n"
        "| `--project P` | proj |\n"
        "| `--type T` | type |\n"
        "| `--content C` | content |\n"
    )

    def test_no_drift_when_doc_matches_argparse(self):
        self.assertEqual(cc.reconcile("tool/fake.py", self.SUBS + self.FLAGS_FULL), [])

    def test_detects_documented_subcommand_missing_from_code(self):
        section = self.SUBS + "| `remove` | gone |\n" + self.FLAGS_FULL
        findings = cc.reconcile("tool/fake.py", section)
        self.assertTrue(any("'remove' not found in argparse" in f for f in findings))

    def test_detects_undocumented_flag_in_code(self):
        # Flag table omits --content, which the real tool defines.
        flags_partial = (
            "\n### Flags\n"
            "| Flag | Description |\n"
            "|------|-------------|\n"
            "| `--project P` | proj |\n"
            "| `--type T` | type |\n"
        )
        findings = cc.reconcile("tool/fake.py", self.SUBS + flags_partial)
        self.assertTrue(any("'--content' exists in argparse but is undocumented" in f for f in findings))

    def test_undocumented_flag_suppressed_when_shown_in_usage(self):
        # --content is shown in a usage example but has no Flag-table row;
        # it should NOT be reported as undocumented (it's mentioned).
        section = self.SUBS + (
            "\nExample: `fake.py add --content C`\n"
            "\n### Flags\n"
            "| Flag | Description |\n"
            "|------|-------------|\n"
            "| `--project P` | proj |\n"
            "| `--type T` | type |\n"
        )
        findings = cc.reconcile("tool/fake.py", section)
        self.assertFalse(any("--content" in f for f in findings))

    def test_stale_flag_uses_table_not_usage_mentions(self):
        # A bash example mentions a foreign flag; it must not count as a stale
        # documented flag (only Flag-table rows are authoritative for 'stale').
        section = self.SUBS + self.FLAGS_FULL + (
            "\n```bash\nfake.py list | grep --color=auto x\n```\n"
        )
        findings = cc.reconcile("tool/fake.py", section)
        self.assertFalse(any("--color" in f for f in findings))

    def test_ignore_marker_suppresses_finding(self):
        flags_partial = (
            "\n### Flags\n"
            "| Flag | Description |\n"
            "|------|-------------|\n"
            "| `--project P` | proj |\n"
            "| `--type T` | type |\n"
            "<!-- cli-claims: ignore: --content -->\n"
        )
        findings = cc.reconcile("tool/fake.py", self.SUBS + flags_partial)
        self.assertEqual(findings, [])

    def test_cannot_introspect_missing_tool_raises(self):
        with self.assertRaises(cc.CannotIntrospect):
            cc.reconcile("tool/nonexistent.py", "### Subcommands\n")


class SectionClassificationTests(unittest.TestCase):
    def test_skip_headers_are_not_tool_sections(self):
        self.assertFalse(cc.is_tool_section("setup.py"))
        self.assertFalse(cc.is_tool_section("runner/config_loader.py"))
        self.assertFalse(cc.is_tool_section("Test scripts"))

    def test_repo_relative_py_paths_are_tool_sections(self):
        self.assertTrue(cc.is_tool_section("scribe/notes.py"))

    def test_bare_name_without_a_path_is_not_a_tool_section(self):
        self.assertFalse(cc.is_tool_section("notes.py"))
        self.assertFalse(cc.is_tool_section("Some prose heading"))


class ConsoleScriptTests(unittest.TestCase):
    """Console scripts are named by command, not by path — they still reconcile."""

    def test_console_scripts_are_tool_sections(self):
        for header in cc.CONSOLE_SCRIPTS:
            with self.subTest(header=header):
                self.assertTrue(cc.is_tool_section(header))

    def test_every_console_script_maps_to_a_file_that_exists(self):
        for header, rel_path in cc.CONSOLE_SCRIPTS.items():
            with self.subTest(header=header):
                self.assertTrue(
                    (cc.REPO_ROOT / rel_path).is_file(),
                    f"{header} maps to missing {rel_path}",
                )

    def test_apiary_resolves_to_core_cli_and_introspects(self):
        # `REPO_ROOT / "apiary"` is not a file, so introspection only works
        # through the mapping. Two subprocesses, not a full introspect().
        base, top_help = cc.resolve_base(cc.CONSOLE_SCRIPTS["apiary"])
        self.assertIn("doctor", cc.help_subcommands(top_help))
        doctor_help = cc._run_help(base + ["doctor"]).stdout
        self.assertIn("--fix", cc.help_flags(doctor_help))

    def test_bare_console_script_name_has_no_file_to_introspect(self):
        with self.assertRaises(cc.CannotIntrospect):
            cc.resolve_base("apiary")

    def test_console_scripts_and_skip_headers_do_not_overlap(self):
        self.assertEqual(set(cc.CONSOLE_SCRIPTS) & cc.SKIP_HEADERS, set())


if __name__ == "__main__":
    unittest.main()
