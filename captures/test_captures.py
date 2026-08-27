"""Tests for the captures subsystem.

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

from captures import cli, store
from core import frontmatter

# Smallest possible valid PNG — 1x1 transparent pixel.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452"
    "00000001000000010806000000"
    "1f15c4890000000d4944415478"
    "9c6300010000000500010d0a2db4"
    "0000000049454e44ae426082"
)


def _make_png(path: Path) -> None:
    path.write_bytes(PNG_1X1)


class CapturesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patch = mock.patch(
            "captures.store._git_repo_root",
            return_value=self.tmp_path,
        )
        self._patch.start()
        self.src_dir = self.tmp_path / "_src"
        self.src_dir.mkdir()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _seed_tags(self, *tags: str) -> None:
        store.ensure_layout()
        store.write_tags(list(tags))

    def _seed_image(self, name: str = "shot.png") -> Path:
        p = self.src_dir / name
        _make_png(p)
        return p


class TestAdd(CapturesTestCase):
    def test_add_copies_image_and_writes_sidecar(self) -> None:
        self._seed_tags("gui-iteration")
        src = self._seed_image()
        code, out, _err = self._run_cli(
            "add", "gui", str(src),
            "--title", "Toolbar v3 dense",
            "--tags", "gui-iteration",
            "--context", "First attempt at dense toolbar.",
        )
        self.assertEqual(code, 0)
        image = self.tmp_path / ".apiary/captures/gui/toolbar-v3-dense.png"
        sidecar = self.tmp_path / ".apiary/captures/gui/toolbar-v3-dense.md"
        self.assertTrue(image.exists())
        self.assertTrue(sidecar.exists())
        self.assertIn(str(image), out)
        self.assertIn(str(sidecar), out)
        # Source should still exist (copy, not move).
        self.assertTrue(src.exists())

        fm, body = store.parse_sidecar(sidecar)
        self.assertEqual(fm["title"], "Toolbar v3 dense")
        self.assertEqual(fm["topic"], "gui")
        self.assertEqual(fm["tags"], ["gui-iteration"])
        self.assertEqual(fm["captured_at"], date.today().isoformat())
        self.assertEqual(fm["image"], "toolbar-v3-dense.png")
        self.assertNotIn("session_id", fm)
        self.assertEqual(fm["related_notes"], [])
        self.assertEqual(fm["sources"], [])
        self.assertIn("First attempt", body)

    def test_add_with_move_removes_source(self) -> None:
        self._seed_tags()
        src = self._seed_image()
        code, _out, _err = self._run_cli(
            "add", "gui", str(src),
            "--title", "Move test",
            "--move",
        )
        self.assertEqual(code, 0)
        self.assertFalse(src.exists())
        dest = self.tmp_path / ".apiary/captures/gui/move-test.png"
        self.assertTrue(dest.exists())

    def test_add_unknown_tag_exits_2_and_writes_nothing(self) -> None:
        self._seed_tags("gui-iteration")
        src = self._seed_image()
        code, _out, err = self._run_cli(
            "add", "gui", str(src),
            "--title", "Has bad tag",
            "--tags", "nope",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("unknown tag", err)
        self.assertFalse((self.tmp_path / ".apiary/captures/gui/has-bad-tag.png").exists())
        self.assertFalse((self.tmp_path / ".apiary/captures/gui/has-bad-tag.md").exists())

    def test_add_unsupported_extension_rejected(self) -> None:
        self._seed_tags()
        bogus = self.src_dir / "shot.tiff"
        bogus.write_bytes(b"not a png")
        code, _out, err = self._run_cli(
            "add", "gui", str(bogus),
            "--title", "Bad ext",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("unsupported image extension", err)

    def test_add_nonexistent_source_rejected(self) -> None:
        self._seed_tags()
        code, _out, err = self._run_cli(
            "add", "gui", "/does/not/exist.png",
            "--title", "Ghost",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("image not found", err)

    def test_add_duplicate_slug_same_topic_exits_2(self) -> None:
        self._seed_tags()
        src = self._seed_image()
        code, _out, _err = self._run_cli(
            "add", "gui", str(src), "--title", "Same title",
        )
        self.assertEqual(code, 0)
        # Second add with same title/topic must fail even with different source.
        src2 = self._seed_image("second.png")
        code, _out, err = self._run_cli(
            "add", "gui", str(src2), "--title", "Same title",
        )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("already exists", err)

    def test_add_stamps_session_and_related_notes(self) -> None:
        self._seed_tags()
        src = self._seed_image()
        code, _out, _err = self._run_cli(
            "add", "gui", str(src),
            "--title", "With refs",
            "--session-id", "abcd1234",
            "--related", "T-2026-209,C-2026-42",
        )
        self.assertEqual(code, 0)
        fm, _ = store.parse_sidecar(
            self.tmp_path / ".apiary/captures/gui/with-refs.md"
        )
        self.assertEqual(fm["session_id"], "abcd1234")
        self.assertEqual(fm["related_notes"], ["T-2026-209", "C-2026-42"])

    def test_add_preserves_case_insensitive_jpg_extension(self) -> None:
        self._seed_tags()
        src = self.src_dir / "Shot.JPG"
        src.write_bytes(PNG_1X1)  # content doesn't matter for ingestion
        code, _out, _err = self._run_cli(
            "add", "gui", str(src), "--title", "Caps",
        )
        self.assertEqual(code, 0)
        dest = self.tmp_path / ".apiary/captures/gui/caps.jpg"
        self.assertTrue(dest.exists())


class TestFind(CapturesTestCase):
    def _add(self, topic: str, title: str, tags: str = "", context: str = "") -> None:
        src = self._seed_image(f"{store.slugify(title)}.png")
        self._run_cli(
            "add", topic, str(src), "--title", title,
            *(["--tags", tags] if tags else []),
            *(["--context", context] if context else []),
        )

    def test_find_ranks_title_matches_highest(self) -> None:
        self._seed_tags("gui-iteration", "ue-viewport")
        self._add("gui", "Toolbar redesign", tags="gui-iteration",
                  context="a screenshot with the word toolbar inside")
        self._add("ue", "Viewport shot", tags="ue-viewport",
                  context="mentions toolbar once in passing")
        code, out, _err = self._run_cli("find", "toolbar")
        self.assertEqual(code, 0)
        # Title match should appear before body-only match.
        first = out.find("Toolbar redesign")
        second = out.find("Viewport shot")
        self.assertGreaterEqual(first, 0)
        self.assertGreaterEqual(second, 0)
        self.assertLess(first, second)

    def test_find_zero_hits_exits_0_with_stderr_hint(self) -> None:
        self._seed_tags()
        code, out, err = self._run_cli("find", "never-seen")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("no captures match", err)


class TestList(CapturesTestCase):
    def test_list_empty_reports_no_captures(self) -> None:
        self._seed_tags()
        code, out, err = self._run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("no captures", err)

    def test_list_groups_by_topic_and_filters(self) -> None:
        self._seed_tags("gui-iteration", "ue-viewport")
        src = self._seed_image("a.png")
        self._run_cli("add", "gui", str(src), "--title", "GUI shot",
                      "--tags", "gui-iteration")
        src2 = self._seed_image("b.png")
        self._run_cli("add", "ue", str(src2), "--title", "UE shot",
                      "--tags", "ue-viewport")
        code, out, _err = self._run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("## gui", out)
        self.assertIn("## ue", out)

        code, out, _err = self._run_cli("list", "--topic", "gui")
        self.assertIn("GUI shot", out)
        self.assertNotIn("UE shot", out)

        code, out, _err = self._run_cli("list", "--tag", "ue-viewport")
        self.assertIn("UE shot", out)
        self.assertNotIn("GUI shot", out)


class TestShowAndPath(CapturesTestCase):
    def test_show_prints_sidecar_and_image_path(self) -> None:
        self._seed_tags()
        src = self._seed_image()
        self._run_cli("add", "gui", str(src), "--title", "Thing")
        code, out, _err = self._run_cli("show", "gui", "thing")
        self.assertEqual(code, 0)
        self.assertIn("title: Thing", out)
        self.assertIn("thing.png", out)

    def test_show_missing_exits_2(self) -> None:
        self._seed_tags()
        code, _out, err = self._run_cli("show", "gui", "ghost")
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("not found", err)

    def test_path_prints_only_image_path(self) -> None:
        self._seed_tags()
        src = self._seed_image()
        self._run_cli("add", "gui", str(src), "--title", "For scripting")
        code, out, _err = self._run_cli("path", "gui", "for-scripting")
        self.assertEqual(code, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("for-scripting.png"))

    def test_path_missing_exits_2(self) -> None:
        self._seed_tags()
        code, _out, err = self._run_cli("path", "gui", "ghost")
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("no image found", err)


class TestRegisterTag(CapturesTestCase):
    def test_register_tag_adds_and_deduplicates(self) -> None:
        code, out, _err = self._run_cli("register-tag", "gui-iteration")
        self.assertEqual(code, 0)
        self.assertIn("gui-iteration", out)

        code, _out, err = self._run_cli("register-tag", "gui-iteration")
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("already registered", err)


class TestStore(CapturesTestCase):
    def test_normalize_topic_kebab_cases(self) -> None:
        self.assertEqual(store.normalize_topic("GUI Iterations"), "gui-iterations")
        self.assertEqual(store.normalize_topic("  __weird__  "), "weird")

    def test_slugify_matches_normalize_topic(self) -> None:
        self.assertEqual(store.slugify("Toolbar v3 Dense"), "toolbar-v3-dense")

    def test_find_image_handles_mixed_case_extensions(self) -> None:
        store.ensure_layout()
        d = store.topic_dir("gui")
        d.mkdir(parents=True)
        f = d / "mixed.JPG"
        f.write_bytes(PNG_1X1)
        found = store.find_image("gui", "mixed")
        self.assertIsNotNone(found)
        self.assertEqual(found.suffix, ".JPG")

    def test_read_tags_raises_on_non_list_root(self) -> None:
        store.ensure_layout()
        store.tags_file().write_text(
            "tags: not_a_list\n", encoding="utf-8"
        )
        with self.assertRaises(frontmatter.FrontmatterError):
            store.read_tags()


if __name__ == "__main__":
    unittest.main()
