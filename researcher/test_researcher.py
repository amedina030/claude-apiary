"""Tests for the researcher subsystem.

Each test isolates state to a fresh ``tempfile.TemporaryDirectory()`` by
monkey-patching the shared ``core.utils.state.git_root`` resolver to return
the temp path. This keeps tests off real user data and avoids depending on
git being runnable.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from core import frontmatter
from researcher import cli, store


class ResearcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patch = mock.patch(
            "core.utils.state.git_root",
            return_value=self.tmp_path,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Invoke ``cli.main`` with captured stdout/stderr."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _seed_tags(self, *tags: str) -> None:
        store.ensure_layout()
        store.write_tags(list(tags))


class TestAdd(ResearcherTestCase):
    def test_add_valid_entry_writes_file_with_populated_frontmatter(self) -> None:
        self._seed_tags("multiplayer", "networking")
        code, out, _err = self._run_cli(
            "add", "unreal", "Replication basics",
            "--tags", "multiplayer,networking",
        )
        self.assertEqual(code, 0)
        path = self.tmp_path / ".apiary/research/unreal/replication-basics.md"
        self.assertIn(str(path), out)
        self.assertTrue(path.exists())
        fm, body = store.parse_entry(path)
        self.assertEqual(fm["title"], "Replication basics")
        self.assertEqual(fm["topic"], "unreal")
        self.assertEqual(fm["tags"], ["multiplayer", "networking"])
        self.assertEqual(fm["date_created"], date.today().isoformat())
        self.assertEqual(fm["date_last_verified"], date.today().isoformat())
        self.assertEqual(fm["sources"], [])
        for section in ("## Summary", "## Context", "## Findings",
                        "## Code / examples", "## Caveats"):
            self.assertIn(section, body)

    def test_add_unknown_tag_exits_2_and_writes_no_file(self) -> None:
        self._seed_tags("multiplayer")
        code, _out, err = self._run_cli(
            "add", "unreal", "Test", "--tags", "foo",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("unknown tag", err)
        path = self.tmp_path / ".apiary/research/unreal/test.md"
        self.assertFalse(path.exists())

    def test_add_duplicate_slug_same_topic_exits_2(self) -> None:
        self._seed_tags("multiplayer")
        self._run_cli("add", "unreal", "Replication basics",
                      "--tags", "multiplayer")
        code, _out, err = self._run_cli(
            "add", "unreal", "Replication basics", "--tags", "multiplayer",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("already exists", err)
        self.assertIn("replication-basics", err)

    def test_add_bootstraps_first_use(self) -> None:
        self._seed_tags()  # creates .apiary/research/ with empty tags
        # Wipe it to simulate truly first-use.
        import shutil
        shutil.rmtree(self.tmp_path / ".apiary")
        # register-tag will bootstrap.
        self._run_cli("register-tag", "first")
        self.assertTrue((self.tmp_path / ".apiary/research").is_dir())
        self.assertTrue((self.tmp_path / ".apiary/research/tags.yaml").is_file())
        code, _out, _err = self._run_cli("add", "unreal", "First entry",
                                         "--tags", "first")
        self.assertEqual(code, 0)
        self.assertTrue(
            (self.tmp_path / ".apiary/research/unreal/first-entry.md").exists()
        )

    def test_add_topic_is_normalized_to_kebab_case(self) -> None:
        self._seed_tags()
        code, out, _err = self._run_cli("add", "Unreal Engine", "Replication basics")
        self.assertEqual(code, 0)
        expected = (self.tmp_path
                    / ".apiary/research/unreal-engine/replication-basics.md")
        self.assertIn(str(expected), out)
        self.assertTrue(expected.exists())

    def test_add_warns_that_body_is_scaffolded(self) -> None:
        self._seed_tags()
        _code, _out, err = self._run_cli("add", "unreal", "Empty findings")
        self.assertIn("warning", err.lower())

    def test_add_allows_two_topics_sharing_a_title(self) -> None:
        self._seed_tags()
        self._run_cli("add", "unreal", "Replication basics")
        code, _out, _err = self._run_cli("add", "godot", "Replication basics")
        self.assertEqual(code, 0)
        self.assertTrue(
            (self.tmp_path / ".apiary/research/unreal/replication-basics.md").exists()
        )
        self.assertTrue(
            (self.tmp_path / ".apiary/research/godot/replication-basics.md").exists()
        )


class TestFind(ResearcherTestCase):
    def _seed_entry(self, topic: str, title: str, tags: list[str]) -> None:
        self._run_cli("add", topic, title,
                      *(["--tags", ",".join(tags)] if tags else []))

    def test_find_returns_matching_entry_with_preview(self) -> None:
        self._seed_tags("multiplayer")
        self._seed_entry("unreal", "Replication basics", ["multiplayer"])
        code, out, _err = self._run_cli("find", "replication")
        self.assertEqual(code, 0)
        self.assertIn("Replication basics", out)
        self.assertIn("multiplayer", out)

    def test_find_no_hits_exits_0_with_message(self) -> None:
        self._seed_tags()
        self._seed_entry("unreal", "Replication basics", [])
        code, _out, err = self._run_cli("find", "quaternions")
        self.assertEqual(code, 0)
        self.assertIn("no matches", err)
        self.assertIn("WebSearch", err)
        self.assertIn("add", err)


class TestList(ResearcherTestCase):
    def test_list_groups_entries_by_topic(self) -> None:
        self._seed_tags("tag1")
        for t in ("a", "b", "c"):
            self._run_cli("add", "topic1", f"Entry {t}", "--tags", "tag1")
        for t in ("x", "y"):
            self._run_cli("add", "topic2", f"Entry {t}", "--tags", "tag1")
        code, out, _err = self._run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("## topic1", out)
        self.assertIn("## topic2", out)
        self.assertIn("Entry a", out)
        self.assertIn("Entry x", out)

    def test_list_filter_by_unregistered_tag_warns_but_returns(self) -> None:
        self._seed_tags("active-tag")
        self._run_cli("add", "unreal", "E", "--tags", "active-tag")
        # Remove the tag from vocab.
        store.write_tags([])
        code, out, err = self._run_cli("list", "--tag", "active-tag")
        self.assertEqual(code, 0)
        self.assertIn("not in controlled vocabulary", err)
        self.assertIn("## unreal", out)


class TestShowVerify(ResearcherTestCase):
    def test_show_prints_full_file_contents(self) -> None:
        self._seed_tags()
        self._run_cli("add", "unreal", "Replication basics")
        code, out, _err = self._run_cli("show", "unreal", "replication-basics")
        self.assertEqual(code, 0)
        self.assertIn("title: Replication basics", out)
        self.assertIn("## Summary", out)

    def test_verify_bumps_date_last_verified(self) -> None:
        self._seed_tags()
        self._run_cli("add", "unreal", "Replication basics")
        path = (self.tmp_path
                / ".apiary/research/unreal/replication-basics.md")
        fm, body = store.parse_entry(path)
        fm["date_last_verified"] = "2026-01-01"
        store.write_entry(path, fm, body)
        code, out, _err = self._run_cli("verify", "unreal", "replication-basics")
        self.assertEqual(code, 0)
        self.assertIn("2026-01-01", out)
        fm2, _body = store.parse_entry(path)
        self.assertEqual(fm2["date_last_verified"], date.today().isoformat())


class TestConfigErrors(ResearcherTestCase):
    def test_invalid_yaml_in_tags_exits_3(self) -> None:
        store.ensure_layout()
        store.tags_file().write_text(
            ": : : not valid yaml\n", encoding="utf-8",
        )
        code, _out, err = self._run_cli("add", "unreal", "Test", "--tags", "x")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("line", err)


class TestRegisterTag(ResearcherTestCase):
    def test_register_tag_appends_and_shows(self) -> None:
        code, out, _err = self._run_cli("register-tag", "multiplayer")
        self.assertEqual(code, 0)
        self.assertIn("multiplayer", out)
        self.assertEqual(store.read_tags(), ["multiplayer"])


class TestFrontmatterContract(unittest.TestCase):
    """The researcher-facing slice of ``core.frontmatter``.

    The dialect itself — quoting, comments, round-trip stability — is
    covered in ``core/test_frontmatter.py``; these two guard the shape
    researcher writes and the error it reports on a bad ``tags.yaml``.
    """

    def test_roundtrip_basic_frontmatter(self) -> None:
        data = {
            "title": "Replication basics",
            "topic": "unreal",
            "tags": ["multiplayer", "networking"],
            "date_created": "2026-04-18",
            "sources": [],
        }
        text = frontmatter.dumps(data)
        parsed = frontmatter.loads(text)
        self.assertEqual(parsed["title"], "Replication basics")
        self.assertEqual(parsed["tags"], ["multiplayer", "networking"])
        self.assertEqual(parsed["sources"], [])

    def test_raises_on_unparseable(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.loads("no colon here\n")


class TestVerifyPreservesFrontmatter(ResearcherTestCase):
    """The live corruption path: `/research verify` on an entry whose title
    holds a colon and whose sources hold URLs with fragments."""

    TITLE = "Claude Code GUI: interactive-wrapper vs Agent-SDK billing"
    SOURCES = ["https://example.com/a#frag", "http://h:8080/p"]

    def test_repeated_verify_does_not_degrade_the_entry(self) -> None:
        self._run_cli("register-tag", "gui")
        code, _out, err = self._run_cli("add", "tools", self.TITLE, "--tags", "gui")
        self.assertEqual(code, 0, err)

        slug = store.slugify(self.TITLE)
        path = store.entry_path(store.normalize_topic("tools"), slug)
        fm, body = store.parse_entry(path)
        fm["sources"] = list(self.SOURCES)
        store.write_entry(path, fm, body)

        for _ in range(3):
            code, _out, err = self._run_cli("verify", "tools", slug)
            self.assertEqual(code, 0, err)
            fm, _body = store.parse_entry(path)
            self.assertEqual(fm["title"], self.TITLE)
            self.assertEqual(fm["sources"], self.SOURCES)
            self.assertEqual(fm["tags"], ["gui"])


if __name__ == "__main__":
    unittest.main()
