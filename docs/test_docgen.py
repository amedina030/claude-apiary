#!/usr/bin/env python3
"""Tests for docs/docgen.py — the sentinel-block and table machinery.

The generators are only trustworthy if the primitives are: a block that cannot
be found must raise rather than silently do nothing, a table round-trip must
not lose a cell, and the merge must keep hand-written prose while letting code
own the row set.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import docgen  # noqa: E402


DOC = """\
# Title

intro prose

<!-- generated:start: demo -->
| Flag | Description |
|------|-------------|
| `--one` | first |
| `--two` | second |
<!-- generated:end: demo -->

trailing prose
"""


class BlockTests(unittest.TestCase):
    def test_block_body_returns_only_the_inner_text(self):
        body = docgen.block_body(DOC, "demo")
        self.assertIn("`--one`", body)
        self.assertNotIn("generated:start", body)
        self.assertNotIn("trailing prose", body)

    def test_missing_block_reads_as_none(self):
        self.assertIsNone(docgen.block_body(DOC, "nope"))

    def test_set_block_replaces_only_the_body(self):
        out = docgen.set_block(DOC, "demo", "replaced")
        self.assertIn("intro prose", out)
        self.assertIn("trailing prose", out)
        self.assertIn("replaced", out)
        self.assertNotIn("`--one`", out)

    def test_set_block_on_a_missing_key_raises(self):
        with self.assertRaises(KeyError):
            docgen.set_block(DOC, "absent", "x")

    def test_set_block_round_trips(self):
        body = docgen.block_body(DOC, "demo")
        self.assertEqual(docgen.set_block(DOC, "demo", body), DOC)

    def test_wrap_region_creates_a_block(self):
        text = "a\nb\nc\n"
        out = docgen.wrap_region(text, "k", 2, 4, "B")
        self.assertIn(docgen.start_marker("k"), out)
        self.assertEqual(docgen.block_body(out, "k"), "B")


class TableTests(unittest.TestCase):
    def test_find_tables_locates_the_table_and_its_rows(self):
        lines = DOC.splitlines()
        found = docgen.find_tables(lines)
        self.assertEqual(len(found), 1)
        _start, _end, table = found[0]
        self.assertEqual(table.headers, ["Flag", "Description"])
        self.assertEqual(len(table.rows), 2)

    def test_escaped_pipes_stay_in_one_cell(self):
        cells = docgen.split_row(r"| `Edit\|Write` | does a thing |")
        self.assertEqual(cells, [r"`Edit\|Write`", "does a thing"])

    def test_render_round_trips_a_parsed_table(self):
        table = docgen.find_tables(DOC.splitlines())[0][2]
        rendered = docgen.render_table(table)
        reparsed = docgen.find_tables(rendered.splitlines())[0][2]
        self.assertEqual(reparsed.rows, table.rows)
        self.assertEqual(reparsed.headers, table.headers)

    def test_cell_key_strips_backticks_and_metavars(self):
        self.assertEqual(docgen.cell_key("`--only HEADER`"), "--only")
        self.assertEqual(docgen.cell_key("`add`"), "add")
        self.assertEqual(docgen.cell_key("`-o` / `--only`"), "-o")

    def test_escape_cell_protects_pipes(self):
        self.assertEqual(docgen.escape_cell("a|b"), r"a\|b")


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.old = docgen.find_tables(DOC.splitlines())[0][2]

    def test_merge_keeps_existing_rows_and_their_order(self):
        merged = docgen.merge_table(self.old, ["--two", "--one"],
                                    ["Flag", "Description"])
        self.assertEqual([r[1] for r in merged.rows], ["first", "second"])

    def test_merge_drops_rows_code_no_longer_has(self):
        merged = docgen.merge_table(self.old, ["--one"], ["Flag", "Description"])
        self.assertEqual(len(merged.rows), 1)
        self.assertEqual(docgen.cell_key(merged.rows[0][0]), "--one")

    def test_merge_appends_new_rows_seeded_from_code(self):
        merged = docgen.merge_table(self.old, ["--one", "--two", "--three"],
                                    ["Flag", "Description"],
                                    seed={"--three": "from argparse"})
        self.assertEqual(docgen.cell_key(merged.rows[-1][0]), "--three")
        self.assertEqual(merged.rows[-1][1], "from argparse")


class SyncTableTests(unittest.TestCase):
    def test_generated_columns_are_overwritten_and_prose_is_kept(self):
        old = docgen.find_tables(DOC.splitlines())[0][2]
        records = [docgen.Record(key="--one", cells={"Flag": "`--one`",
                                                     "Description": "seed"})]
        table = docgen.sync_table(old, records, ["Flag", "Description"],
                                  generated=["Flag"])
        self.assertEqual(table.rows, [["`--one`", "first"]])

    def test_a_record_with_no_existing_row_uses_its_seed(self):
        records = [docgen.Record(key="--new", cells={"Flag": "`--new`",
                                                     "Description": "seed"})]
        table = docgen.sync_table(None, records, ["Flag", "Description"],
                                  generated=["Flag"])
        self.assertEqual(table.rows, [["`--new`", "seed"]])

    def test_row_order_follows_code_not_the_document(self):
        old = docgen.find_tables(DOC.splitlines())[0][2]
        records = [docgen.Record(key="--two", cells={"Flag": "`--two`"}),
                   docgen.Record(key="--one", cells={"Flag": "`--one`"})]
        table = docgen.sync_table(old, records, ["Flag", "Description"],
                                  generated=["Flag"])
        self.assertEqual([docgen.cell_key(r[0]) for r in table.rows],
                         ["--two", "--one"])


class HarnessTests(unittest.TestCase):
    def _gen(self, path: Path, build):
        return [docgen.Generator(path, build)]

    def test_check_reports_drift_and_exits_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "doc.md"
            p.write_text(DOC, encoding="utf-8")
            code = docgen.run_generators(
                self._gen(p, lambda t: t.replace("first", "FIRST")),
                description="t", argv=["--check"])
            self.assertEqual(code, 1)
            self.assertIn("first", p.read_text(encoding="utf-8"))

    def test_write_rewrites_and_exits_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "doc.md"
            p.write_text(DOC, encoding="utf-8")
            code = docgen.run_generators(
                self._gen(p, lambda t: t.replace("first", "FIRST")),
                description="t", argv=["--write"])
            self.assertEqual(code, 0)
            self.assertIn("FIRST", p.read_text(encoding="utf-8"))

    def test_missing_file_exits_two(self):
        code = docgen.run_generators(
            self._gen(Path("does-not-exist.md"), lambda t: t),
            description="t", argv=["--check"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
