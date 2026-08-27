"""Tests for scripts/migrate_frontmatter.py.

Every case builds its own temp state dir — nothing here reads or writes the
live store.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import frontmatter  # noqa: E402
from scripts import migrate_frontmatter as mig  # noqa: E402


class MigrateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel: str, text: str) -> Path:
        path = self.state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mig.main(["--state-dir", str(self.state), *args])
        return code, buf.getvalue()

    def family(self, name: str) -> mig.Family:
        return next(f for f in mig.FAMILIES if f.name == name)


class TestDiscovery(MigrateTestCase):
    def test_each_family_finds_its_own_files(self) -> None:
        self.write("repo-1/scribe/learnings/2026/1.md", "---\ntags: [a]\n---\nbody\n")
        self.write("repo-1/scribe/templates/handoff.md", "---\nrequired: [A]\n---\nx\n")
        self.write("repo-1/scribe/memory/note.md", "---\nname: N\n---\nx\n")
        self.write("repo-1/research/gui/e.md", "---\ntitle: T\n---\nx\n")
        self.write("repo-1/captures/gui/c.md", "---\ntitle: T\n---\nx\n")
        for name, expected in (
            ("learnings", 1),
            ("templates", 1),
            ("memory", 1),
            ("research", 1),
            ("captures", 1),
        ):
            with self.subTest(family=name):
                found = list(mig.iter_files(self.state, self.family(name)))
                self.assertEqual(len(found), expected)

    def test_backup_snapshots_are_never_walked(self) -> None:
        self.write("repo-1/scribe/backup/20260414Z/memory/old.md", "---\nname: N\n---\nx\n")
        self.assertEqual(list(mig.iter_files(self.state, self.family("memory"))), [])

    def test_legacy_researcher_dir_name_is_accepted(self) -> None:
        self.write("repo-1/researcher/gui/e.md", "---\ntitle: T\n---\nx\n")
        self.assertEqual(len(list(mig.iter_files(self.state, self.family("research")))), 1)


class TestExamine(MigrateTestCase):
    def test_canonical_file_needs_nothing(self) -> None:
        path = self.write(
            "r/scribe/learnings/2026/1.md", "---\ntags: [a, b]\n---\nbody\n"
        )
        self.assertEqual(mig.examine(path, self.family("learnings")).status, "canonical")

    def test_file_without_frontmatter(self) -> None:
        path = self.write("r/scribe/learnings/2026/2.md", "just a body\n")
        self.assertEqual(
            mig.examine(path, self.family("learnings")).status, "no-frontmatter"
        )

    def test_normalizable_file_is_rewritable(self) -> None:
        """Missing space after the comma — same meaning, different bytes."""
        path = self.write(
            "r/scribe/learnings/2026/3.md", "---\ntags: [a,b]\n---\nbody\n"
        )
        self.assertEqual(mig.examine(path, self.family("learnings")).status, "rewritable")

    def test_memory_files_are_new_coverage(self) -> None:
        """Nothing parsed memory before Phase 3.3, so there is nothing to diff."""
        path = self.write("r/scribe/memory/m.md", "---\nname: N\ntype: feedback\n---\nx\n")
        self.assertEqual(mig.examine(path, self.family("memory")).status, "new-coverage")

    def test_real_disagreement_is_reported(self) -> None:
        """The legacy scribe splitter ignored quotes; the new one does not."""
        path = self.write(
            "r/scribe/learnings/2026/4.md", '---\ntags: [a, "b, c"]\n---\nbody\n'
        )
        result = mig.examine(path, self.family("learnings"))
        self.assertEqual(result.status, "differs")
        self.assertIn("tags", result.detail)

    def test_unreadable_file_is_reported_not_raised(self) -> None:
        path = self.write("r/scribe/learnings/2026/5.md", "x")
        path.write_bytes(b"---\ntags: [\xff\xfe]\n---\n")
        self.assertEqual(mig.examine(path, self.family("learnings")).status, "unreadable")


class TestCheckMode(MigrateTestCase):
    def test_clean_store_exits_zero_and_writes_nothing(self) -> None:
        path = self.write("r/scribe/learnings/2026/1.md", "---\ntags: [a, b]\n---\nbody\n")
        before = path.read_bytes()
        code, out = self.run_cli("--check")
        self.assertEqual(code, mig.EXIT_OK)
        self.assertIn("0 file(s) need review", out)
        self.assertEqual(path.read_bytes(), before)

    def test_check_is_the_default_mode(self) -> None:
        path = self.write("r/scribe/learnings/2026/1.md", "---\ntags: [a,b]\n---\nbody\n")
        before = path.read_bytes()
        code, out = self.run_cli()
        self.assertEqual(code, mig.EXIT_OK)
        self.assertIn("frontmatter check", out)
        self.assertEqual(path.read_bytes(), before)

    def test_disagreement_exits_one_without_writing(self) -> None:
        path = self.write(
            "r/scribe/learnings/2026/1.md", '---\ntags: [a, "b, c"]\n---\nbody\n'
        )
        before = path.read_bytes()
        code, out = self.run_cli("--check")
        self.assertEqual(code, mig.EXIT_DIFF)
        self.assertIn("[differs]", out)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_state_dir_is_a_usage_error(self) -> None:
        code = mig.main(["--state-dir", str(self.state / "nope")])
        self.assertEqual(code, mig.EXIT_USAGE)

    def test_family_filter(self) -> None:
        self.write("r/scribe/learnings/2026/1.md", "---\ntags: [a,b]\n---\nbody\n")
        self.write("r/research/gui/e.md", "---\ntitle: T\n---\nx\n")
        _code, out = self.run_cli("--check", "--family", "learnings")
        self.assertIn("learnings:", out)
        self.assertNotIn("research:", out)


class TestApplyMode(MigrateTestCase):
    def test_apply_normalizes_and_preserves_the_body(self) -> None:
        body = "Line one.\n\n  indented\n"
        path = self.write("r/scribe/learnings/2026/1.md", f"---\ntags: [a,b]\n---\n{body}")
        code, out = self.run_cli("--apply")
        self.assertEqual(code, mig.EXIT_OK)
        self.assertIn("rewrote 1 file(s)", out)
        meta, new_body = frontmatter.parse(path.read_text(encoding="utf-8"))
        self.assertEqual(meta, {"tags": ["a", "b"]})
        self.assertEqual(new_body, body)
        self.assertIn("tags: [a, b]", path.read_text(encoding="utf-8"))

    def test_apply_keeps_the_family_list_style(self) -> None:
        self.write("r/scribe/learnings/2026/1.md", "---\ntags: [a,b]\n---\nx\n")
        research = self.write(
            "r/research/gui/e.md", "---\ntitle: A: B\ntags:\n  - a\n  - b\n---\nx\n"
        )
        self.run_cli("--apply")
        learning_text = (self.state / "r/scribe/learnings/2026/1.md").read_text(
            encoding="utf-8"
        )
        research_text = research.read_text(encoding="utf-8")
        self.assertIn("tags: [a, b]", learning_text)
        self.assertIn("tags:\n  - a\n  - b\n", research_text)
        self.assertIn('title: "A: B"', research_text)

    def test_inline_list_in_a_research_entry_is_a_real_disagreement(self) -> None:
        """The exact cross-dialect incompatibility knowledge.md §3 probed.

        ``_yaml_mini`` read ``tags: [a, b]`` as the *string* ``'[a, b]'``; the
        new dialect reads a list. A research entry in that shape must be
        reported, never silently rewritten.
        """
        path = self.write("r/research/gui/e.md", "---\ntags: [a, b]\n---\nx\n")
        before = path.read_bytes()
        code, out = self.run_cli("--apply")
        self.assertEqual(code, mig.EXIT_DIFF)
        self.assertIn("[differs]", out)
        self.assertEqual(path.read_bytes(), before)

    def test_apply_refuses_a_file_whose_parses_disagree(self) -> None:
        path = self.write(
            "r/scribe/learnings/2026/1.md", '---\ntags: [a, "b, c"]\n---\nbody\n'
        )
        before = path.read_bytes()
        code, _out = self.run_cli("--apply")
        self.assertEqual(code, mig.EXIT_DIFF)
        self.assertEqual(path.read_bytes(), before)

    def test_apply_leaves_files_without_frontmatter_alone(self) -> None:
        path = self.write("r/scribe/learnings/2026/1.md", "just a body\n")
        before = path.read_bytes()
        self.run_cli("--apply")
        self.assertEqual(path.read_bytes(), before)

    def test_apply_is_idempotent(self) -> None:
        path = self.write(
            "r/research/gui/e.md", "---\ntitle: A: B\ntags:\n  - a\n  - b\n---\nx\n"
        )
        self.run_cli("--apply")
        once = path.read_bytes()
        code, out = self.run_cli("--apply")
        self.assertEqual(code, mig.EXIT_OK)
        self.assertIn("rewrote 0 file(s)", out)
        self.assertEqual(path.read_bytes(), once)


class TestLegacyParsersAreFrozen(MigrateTestCase):
    """The frozen copies must still reproduce the *old* behaviour, bugs included.

    If someone "fixes" them, --check silently stops detecting anything.
    """

    def test_legacy_scribe_splits_inside_quotes(self) -> None:
        meta, _ = mig._legacy_scribe('---\ntags: [a, "b, c"]\n---\nbody\n')
        self.assertEqual(meta["tags"], ["a", "b", "c"])

    def test_legacy_scribe_drops_block_lists(self) -> None:
        meta, _ = mig._legacy_scribe("---\ntags:\n  - a\n  - b\n---\nbody\n")
        self.assertEqual(meta, {"tags": ""})

    def test_legacy_yaml_mini_reads_an_inline_list_as_a_string(self) -> None:
        self.assertEqual(mig._legacy_yaml_mini_loads("tags: [a, b]"), {"tags": "[a, b]"})

    def test_legacy_sidecar_returns_none_where_it_used_to_raise(self) -> None:
        self.assertIsNone(mig._legacy_sidecar("no fence\n"))
        self.assertIsNone(mig._legacy_sidecar("---\ntags: [a]\nnever closed\n"))

    def test_memory_has_no_legacy_parser(self) -> None:
        self.assertIsNone(mig._legacy_none("---\nname: N\n---\nx\n"))


if __name__ == "__main__":
    unittest.main()
