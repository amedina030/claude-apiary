"""Tests for the researcher subsystem.

Each test isolates state to a fresh ``tempfile.TemporaryDirectory()`` by
monkey-patching ``store._git_repo_root`` to return the temp path. This
keeps tests off real user data and avoids depending on git being runnable.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from researcher import _yaml_mini, cli, store


class ResearcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patch = mock.patch(
            "researcher.store._git_repo_root",
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


class TestYamlMini(unittest.TestCase):
    def test_roundtrip_basic_frontmatter(self) -> None:
        data = {
            "title": "Replication basics",
            "topic": "unreal",
            "tags": ["multiplayer", "networking"],
            "date_created": "2026-04-18",
            "sources": [],
        }
        text = _yaml_mini.dumps(data)
        parsed = _yaml_mini.loads(text)
        self.assertEqual(parsed["title"], "Replication basics")
        self.assertEqual(parsed["tags"], ["multiplayer", "networking"])
        self.assertEqual(parsed["sources"], [])

    def test_raises_on_unparseable(self) -> None:
        with self.assertRaises(_yaml_mini.YamlParseError):
            _yaml_mini.loads("no colon here\n")


class TestYamlMiniRoundTrip(unittest.TestCase):
    """`dumps` → `loads` must be lossless (deep review 2026-08, knowledge §3.2).

    The old reader quoted on write but never unquoted on read, and treated any
    ``#`` as a comment. `/research verify` is exactly ``loads → mutate → dumps``,
    so every verify degraded a title with a colon or a source URL with a
    fragment, compounding each time.
    """

    # Values that must survive dump → load unchanged. The first three are the
    # exact inputs recorded in docs/review/subsystems/knowledge.md §3.2.
    VALUES = (
        "Foo: bar",
        "C# generics",
        "https://example.com/a#frag",
        "https://x/y#frag",
        "http://h:8080/p",
        "issue#12",
        "Claude Code GUI: interactive-wrapper vs Agent-SDK billing",
        'say "hi"',
        "it's fine",
        '"already quoted"',
        "'single quoted'",
        "a\"b'c",
        "trailing hash #",
        "# leading hash",
        "- leading dash",
        "back\\slash",
        "  padded  ",
        "",
        "[]",
        "{}",
        "plain value",
    )

    def _round_trip(self, value: str) -> str:
        return _yaml_mini.loads(_yaml_mini.dumps({"title": value}))["title"]

    def test_scalar_round_trip(self) -> None:
        for value in self.VALUES:
            with self.subTest(value=value):
                self.assertEqual(self._round_trip(value), value)

    def test_list_item_round_trip(self) -> None:
        for value in self.VALUES:
            with self.subTest(value=value):
                data = {"sources": [value]}
                parsed = _yaml_mini.loads(_yaml_mini.dumps(data))
                self.assertEqual(parsed["sources"], [value])

    def test_repeated_round_trips_are_stable(self) -> None:
        """`verify` re-reads and re-writes; three passes must not compound."""
        for value in self.VALUES:
            with self.subTest(value=value):
                current = value
                for _ in range(3):
                    current = self._round_trip(current)
                self.assertEqual(current, value)

    def test_full_document_round_trip(self) -> None:
        data = {
            "title": "Claude Code GUI: interactive-wrapper vs Agent-SDK billing",
            "topic": "unreal",
            "tags": ["multiplayer", "c#"],
            "date_created": "2026-04-18",
            "date_last_verified": "2026-08-26",
            "sources": [
                "https://example.com/a#frag",
                "http://h:8080/p",
                "https://docs.example.com/guide#section-2",
            ],
        }
        self.assertEqual(_yaml_mini.loads(_yaml_mini.dumps(data)), data)

    def test_empty_list_stays_a_list_but_quoted_brackets_stay_a_string(self) -> None:
        parsed = _yaml_mini.loads('tags: []\nnote: "[]"\n')
        self.assertEqual(parsed["tags"], [])
        self.assertEqual(parsed["note"], "[]")

    def test_bare_key_still_opens_a_block_list(self) -> None:
        parsed = _yaml_mini.loads("tags:\n  - a\n  - b\n")
        self.assertEqual(parsed["tags"], ["a", "b"])


class TestYamlMiniComments(unittest.TestCase):
    def test_hash_after_whitespace_starts_a_comment(self) -> None:
        self.assertEqual(_yaml_mini.loads("title: value # note\n")["title"], "value")

    def test_hash_at_line_start_is_a_whole_line_comment(self) -> None:
        parsed = _yaml_mini.loads("# a comment\ntitle: value\n  # indented comment\n")
        self.assertEqual(parsed, {"title": "value"})

    def test_hash_inside_a_word_is_not_a_comment(self) -> None:
        for raw, expected in (
            ("title: C# generics\n", "C# generics"),
            ("title: issue#12\n", "issue#12"),
            ("title: https://example.com/a#frag\n", "https://example.com/a#frag"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_yaml_mini.loads(raw)["title"], expected)

    def test_comment_after_a_quoted_value_is_stripped(self) -> None:
        self.assertEqual(_yaml_mini.loads('title: "a # b"  # note\n')["title"], "a # b")

    def test_comment_inside_a_list_item(self) -> None:
        parsed = _yaml_mini.loads("sources:\n  - https://x/y#frag  # keep\n")
        self.assertEqual(parsed["sources"], ["https://x/y#frag"])


class TestYamlMiniQuotes(unittest.TestCase):
    def test_symmetric_quotes_are_stripped(self) -> None:
        self.assertEqual(_yaml_mini.loads('title: "Foo: bar"\n')["title"], "Foo: bar")
        self.assertEqual(_yaml_mini.loads("title: 'Foo: bar'\n")["title"], "Foo: bar")

    def test_quotes_inside_a_quoted_value_are_preserved(self) -> None:
        self.assertEqual(
            _yaml_mini.loads('title: "he said \\"hi\\""\n')["title"], 'he said "hi"'
        )
        self.assertEqual(_yaml_mini.loads("title: 'it''s'\n")["title"], "it's")

    def test_value_that_merely_contains_a_quote_is_untouched(self) -> None:
        for raw, expected in (
            ('title: say "hi"\n', 'say "hi"'),
            ("title: it's fine\n", "it's fine"),
            ('title: a"b\n', 'a"b'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_yaml_mini.loads(raw)["title"], expected)

    def test_unterminated_quote_is_kept_verbatim(self) -> None:
        self.assertEqual(_yaml_mini.loads('title: "unclosed\n')["title"], '"unclosed')

    def test_mismatched_quote_pair_is_kept_verbatim(self) -> None:
        self.assertEqual(_yaml_mini.loads("title: \"mixed'\n")["title"], "\"mixed'")


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
