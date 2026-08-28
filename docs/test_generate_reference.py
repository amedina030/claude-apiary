#!/usr/bin/env python3
"""Tests for docs/generate_reference.py.

The generated blocks are checked for staleness here as well as in the
pre-commit hook, so a doc cannot rot between commits. The hand-written tables
that sit *next to* generated ones (the descriptive columns) are checked for
coverage: every registered hook has a description, every command file has a
row, every config key the code reads is documented.
"""

import json
import sys
import unittest
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
for _p in (str(DOCS_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import docgen  # noqa: E402
import generate_reference as gen  # noqa: E402


class InSyncTests(unittest.TestCase):
    def test_every_generated_block_is_up_to_date(self):
        for g in gen.generators():
            with self.subTest(doc=g.rel()):
                current = g.path.read_text(encoding="utf-8")
                self.assertEqual(
                    g.build(current), current,
                    f"{g.rel()} is stale — run "
                    f"`python docs/generate_reference.py --write`")


class HooksTableTests(unittest.TestCase):
    def setUp(self):
        self.text = gen.HOOKS_DOC.read_text(encoding="utf-8")
        self.table = docgen.first_table(
            docgen.block_body(self.text, "hooks:registry"))

    def test_one_row_per_registered_hook_in_dispatch_order(self):
        expected = [r.key for r in gen.hook_registry_records()]
        actual = [gen._registry_row_key(row, self.table.headers)
                  for row in self.table.rows]
        self.assertEqual(actual, expected)

    def test_every_hook_has_a_hand_written_description(self):
        col = self.table.index_of("what it does")
        self.assertIsNotNone(col)
        for row in self.table.rows:
            with self.subTest(hook=row[2]):
                self.assertTrue(row[col].strip(),
                                f"{row[2]} has no description in hooks.md")

    def test_every_lifecycle_event_is_documented(self):
        events = docgen.first_table(docgen.block_body(self.text, "hooks:events"))
        col = events.index_of("when it fires")
        for row in events.rows:
            with self.subTest(event=row[0]):
                self.assertTrue(row[col].strip())

    def test_every_hook_module_named_in_the_table_exists(self):
        for row in self.table.rows:
            rel = row[3].strip("` ")
            with self.subTest(module=rel):
                self.assertTrue((REPO_ROOT / rel).is_file())


class CommandTableTests(unittest.TestCase):
    def test_every_command_file_has_a_row(self):
        text = gen.COMMANDS_DOC.read_text(encoding="utf-8")
        table = docgen.first_table(docgen.block_body(text, "slash-commands"))
        listed = {docgen.cell_key(r[0]) for r in table.rows}
        expected = {r.key for r in gen.command_records()}
        self.assertEqual(listed, expected)

    def test_every_command_file_declares_a_name_and_description(self):
        from core import frontmatter
        for path in sorted(REPO_ROOT.glob("*/commands/*.md")):
            with self.subTest(command=path.name):
                fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
                self.assertTrue(fm, f"{path} has no frontmatter")
                self.assertTrue(fm.get("name"), f"{path} has no name:")
                self.assertTrue(fm.get("description"),
                                f"{path} has no description:")

    def test_source_paths_point_at_real_files(self):
        text = gen.COMMANDS_DOC.read_text(encoding="utf-8")
        table = docgen.first_table(docgen.block_body(text, "slash-commands"))
        col = table.index_of("source")
        for row in table.rows:
            rel = row[col].strip("` ")
            with self.subTest(source=rel):
                self.assertTrue((REPO_ROOT / rel).is_file())


class ConfigTableTests(unittest.TestCase):
    def test_every_shipped_key_is_documented(self):
        text = gen.CONFIG_DOC.read_text(encoding="utf-8")
        for rel in gen.CONFIG_FILES:
            table = docgen.first_table(docgen.block_body(text, f"config:{rel}"))
            keys = {gen._config_row_key(r, table.headers) for r in table.rows}
            with self.subTest(config=rel):
                for rec in gen.config_records(rel):
                    self.assertIn(rec.key, keys)

    def test_every_documented_key_has_a_description(self):
        text = gen.CONFIG_DOC.read_text(encoding="utf-8")
        for rel in gen.CONFIG_FILES:
            table = docgen.first_table(docgen.block_body(text, f"config:{rel}"))
            col = table.index_of("description")
            for row in table.rows:
                with self.subTest(config=rel, key=row[0]):
                    self.assertTrue(row[col].strip())

    def test_defaults_match_the_shipped_files(self):
        text = gen.CONFIG_DOC.read_text(encoding="utf-8")
        for rel in gen.CONFIG_FILES:
            data = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
            table = docgen.first_table(docgen.block_body(text, f"config:{rel}"))
            documented = {gen._config_row_key(r, table.headers):
                          r[table.index_of("default")] for r in table.rows}
            for rec in gen.config_records(rel):
                with self.subTest(config=rel, key=rec.key):
                    self.assertEqual(documented[rec.key],
                                     rec.cells["Default"])
            self.assertTrue(data)


class HandWrittenConfigTableTests(unittest.TestCase):
    """The config tables that are NOT generated, tested instead.

    `launch.json`'s defaults live in `gui/theme.py::DEFAULT_LAUNCH`, which the
    generator does not read (it would import the gui package for one table).
    So the claim is checked here: every field documented, every default right,
    and nothing documented that the whitelist would drop on load.
    """

    def setUp(self):
        from gui.theme import DEFAULT_LAUNCH
        self.defaults = DEFAULT_LAUNCH
        text = gen.CONFIG_DOC.read_text(encoding="utf-8")
        section = text.split("launch.json", 1)[1]
        self.table = docgen.first_table(section)

    def _documented(self) -> dict[str, str]:
        col = self.table.index_of("default")
        return {docgen.cell_key(r[0]): r[col] for r in self.table.rows}

    def test_every_field_is_documented(self):
        self.assertEqual(set(self._documented()), set(self.defaults))

    def test_every_documented_default_matches_the_code(self):
        for key, shown in self._documented().items():
            with self.subTest(field=key):
                self.assertEqual(shown, gen._render_default(self.defaults[key]))


class StorageTableTests(unittest.TestCase):
    def test_no_machine_specific_path_leaks_into_the_doc(self):
        text = gen.STORAGE_DOC.read_text(encoding="utf-8")
        body = docgen.block_body(text, "storage:paths")
        for row in docgen.first_table(body).rows:
            with self.subTest(path=row[1]):
                self.assertNotIn(":\\", row[1])
                self.assertFalse(row[1].strip("` ").startswith("/"))

    def test_state_paths_are_relative_to_the_state_dir(self):
        rows = {r.key: r.cells["Path"] for r in gen.storage_records()}
        self.assertTrue(rows["scribe"].startswith(f"`{gen.STATE}/"))
        self.assertTrue(rows["runner/intake"].startswith(f"`{gen.STATE}/"))

    def test_runner_worktrees_live_next_to_the_repo_not_in_state(self):
        rows = {r.key: r.cells["Path"] for r in gen.storage_records()}
        self.assertTrue(rows["runner worktrees"].startswith(f"`{gen.REPO}/"))

    def test_no_row_reads_a_global_a_test_may_have_redirected(self):
        # budgeter's LOG_PATH/TMP_DIR are rebound by configure_for_project(),
        # so reading them inside the suite printed a pytest tmpdir into the
        # doc. Every row has to survive being generated mid-suite.
        from budgeter.lib import logger
        saved = (logger.LOG_PATH, logger.TMP_DIR)
        logger.LOG_PATH = Path("/somewhere/else/log.jsonl")
        logger.TMP_DIR = Path("/somewhere/else/tmp")
        try:
            rows = {r.key: r.cells["Path"] for r in gen.storage_records()}
        finally:
            logger.LOG_PATH, logger.TMP_DIR = saved
        self.assertIn("budgeter/data", rows["budgeter log"])
        self.assertNotIn("somewhere/else", rows["budgeter log"])


class ArchivePolicyTests(unittest.TestCase):
    def test_the_table_matches_scribe_policy(self):
        import scribe.policy as policy
        text = gen.SCRIBE_DOC.read_text(encoding="utf-8")
        body = docgen.block_body(text, "scribe:archive-policy")
        self.assertIn(f"{policy.CONTEXT_RETENTION_DAYS} days old", body)
        self.assertIn(f"{policy.DECISION_RETENTION_DAYS} days old", body)
        self.assertIn(f"{policy.DONE_RETENTION_DAYS} day after", body)

    def test_types_with_no_age_rule_are_listed_as_never(self):
        import scribe.policy as policy
        from scribe.store import VALID_TYPES
        text = gen.SCRIBE_DOC.read_text(encoding="utf-8")
        body = docgen.block_body(text, "scribe:archive-policy")
        for note_type in VALID_TYPES:
            if note_type in policy._AGE_RULES or note_type == "handoff":
                continue
            with self.subTest(note_type=note_type):
                self.assertIn(f"`{note_type}`", body)


if __name__ == "__main__":
    unittest.main()
