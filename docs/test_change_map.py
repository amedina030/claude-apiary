#!/usr/bin/env python3
"""Tests for docs/change_map.py — the code-changed-without-its-doc gate.

Also validates the map itself: an entry whose globs match nothing is a rule
that silently stopped applying, which is the failure mode this whole phase
exists to prevent.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
sys.path.insert(0, str(DOCS_DIR))
import change_map as cm  # noqa: E402

ENTRIES = [
    {"id": "hooks", "code": ["core/hooks/*.py"],
     "docs": ["docs/reference/hooks.md"], "why": "because"},
    {"id": "state", "code": ["core/utils/state.py"],
     "docs": ["docs/reference/file-storage.md"]},
]


class MappingTests(unittest.TestCase):
    def test_code_without_its_doc_is_a_finding(self):
        found = cm.mapping_findings(["core/hooks/dispatch.py"], ENTRIES)
        self.assertEqual(len(found), 1)
        self.assertIn("hooks", found[0])
        self.assertIn("docs/reference/hooks.md", found[0])

    def test_code_with_its_doc_is_clean(self):
        found = cm.mapping_findings(
            ["core/hooks/dispatch.py", "docs/reference/hooks.md"], ENTRIES)
        self.assertEqual(found, [])

    def test_an_unmapped_file_is_ignored(self):
        self.assertEqual(cm.mapping_findings(["gui/app.js"], ENTRIES), [])

    def test_each_entry_is_reported_separately(self):
        found = cm.mapping_findings(
            ["core/hooks/dispatch.py", "core/utils/state.py"], ENTRIES)
        self.assertEqual(len(found), 2)

    def test_the_why_is_shown(self):
        found = cm.mapping_findings(["core/hooks/dispatch.py"], ENTRIES)
        self.assertIn("because", found[0])

    def test_many_touched_files_are_summarised(self):
        paths = [f"core/hooks/h{i}.py" for i in range(6)]
        found = cm.mapping_findings(paths, ENTRIES)
        self.assertIn("(+3)", found[0])


class WaiverTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(cm.ENV_ESCAPE, None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(cm.ENV_ESCAPE, None)
        else:
            os.environ[cm.ENV_ESCAPE] = self._saved

    def test_the_trailer_waives(self):
        self.assertTrue(cm.waived("fix: a thing\n\ndocs: unchanged\n"))

    def test_the_trailer_is_case_insensitive_and_whitespace_tolerant(self):
        self.assertTrue(cm.waived("x\n\n  Docs:   Unchanged  \n"))

    def test_a_mention_inside_prose_does_not_waive(self):
        self.assertFalse(cm.waived("this commit left the docs: unchanged for now"))

    def test_no_message_does_not_waive(self):
        self.assertFalse(cm.waived(""))

    def test_the_env_var_waives(self):
        os.environ[cm.ENV_ESCAPE] = "1"
        self.assertTrue(cm.waived(""))

    def test_a_falsey_env_var_does_not_waive(self):
        for value in ("", "0", "false", "no"):
            os.environ[cm.ENV_ESCAPE] = value
            with self.subTest(value=value):
                self.assertFalse(cm.waived(""))


class StagedDocFreshnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "docs" / "reference").mkdir(parents=True)
        self._saved_root = cm.REPO_ROOT
        cm.REPO_ROOT = self.root
        self.addCleanup(self._restore)

    def _restore(self):
        cm.REPO_ROOT = self._saved_root
        self._tmp.cleanup()

    def _doc(self, rel: str, last_verified: str):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntitle: x\nlast_verified: "{last_verified}"\n---\n\nbody\n',
                     encoding="utf-8")
        return rel

    def test_an_old_stamp_on_a_staged_doc_fails(self):
        rel = self._doc("docs/reference/x.md", "2026-01-01")
        found = cm.stale_doc_findings([rel], today="2026-08-27")
        self.assertEqual(len(found), 1)
        self.assertIn("2026-01-01", found[0])

    def test_todays_stamp_passes(self):
        rel = self._doc("docs/reference/x.md", "2026-08-27")
        self.assertEqual(cm.stale_doc_findings([rel], today="2026-08-27"), [])

    def test_a_doc_with_no_frontmatter_is_left_to_check_py(self):
        p = self.root / "docs" / "reference" / "y.md"
        p.write_text("no frontmatter\n", encoding="utf-8")
        self.assertEqual(cm.stale_doc_findings(["docs/reference/y.md"]), [])

    def test_the_review_snapshots_are_exempt(self):
        rel = self._doc("docs/review/old.md", "2026-01-01")
        self.assertEqual(cm.stale_doc_findings([rel], today="2026-08-27"), [])

    def test_a_non_doc_path_is_ignored(self):
        self.assertEqual(cm.stale_doc_findings(["core/cli.py"]), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(cm.ENV_ESCAPE, None)
        self.addCleanup(
            lambda: os.environ.__setitem__(cm.ENV_ESCAPE, self._saved)
            if self._saved is not None else os.environ.pop(cm.ENV_ESCAPE, None))

    def test_list_prints_the_map_and_exits_zero(self):
        self.assertEqual(cm.main(["--list"]), 0)

    def test_paths_mode_blocks_and_exits_one(self):
        self.assertEqual(cm.main(["--paths", "core/hooks/dispatch.py"]), 1)

    def test_paths_mode_with_the_doc_exits_zero(self):
        self.assertEqual(
            cm.main(["--paths", "core/hooks/dispatch.py", "docs/reference/hooks.md"]), 0)

    def test_the_env_escape_lets_it_through(self):
        os.environ[cm.ENV_ESCAPE] = "1"
        self.assertEqual(cm.main(["--paths", "core/hooks/dispatch.py"]), 0)

    def test_the_trailer_lets_it_through(self):
        with tempfile.TemporaryDirectory() as td:
            msg = Path(td) / "m.txt"
            msg.write_text("fix: x\n\ndocs: unchanged\n", encoding="utf-8")
            self.assertEqual(
                cm.main(["--paths", "core/hooks/dispatch.py", "--message", str(msg)]), 0)

    def test_no_paths_at_all_exits_zero(self):
        self.assertEqual(cm.main(["--paths"]), 0)


class TheRealMapTests(unittest.TestCase):
    """The shipped map has to keep applying to the tree it describes."""

    @classmethod
    def setUpClass(cls):
        cls.entries = cm.load_map()
        cls.tracked = {p.relative_to(REPO_ROOT).as_posix()
                       for p in REPO_ROOT.rglob("*")
                       if p.is_file() and ".git" not in p.parts}

    def test_every_entry_matches_at_least_one_real_file(self):
        for entry in self.entries:
            with self.subTest(entry=entry["id"]):
                self.assertTrue(
                    any(cm.matches(p, entry["code"]) for p in self.tracked),
                    f"{entry['id']}: no file matches {entry['code']} any more")

    def test_every_mapped_doc_exists(self):
        for entry in self.entries:
            for pattern in entry["docs"]:
                with self.subTest(entry=entry["id"], doc=pattern):
                    self.assertTrue(
                        any(cm.matches(p, [pattern]) for p in self.tracked),
                        f"{entry['id']} maps to {pattern}, which does not exist")

    def test_every_entry_explains_itself(self):
        for entry in self.entries:
            with self.subTest(entry=entry["id"]):
                self.assertTrue(entry.get("why"), "an entry with no 'why' is friction "
                                                  "nobody can evaluate later")

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
