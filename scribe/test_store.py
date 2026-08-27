#!/usr/bin/env python3
"""Unit tests for scribe.store — folder-per-type storage engine."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import (
    ARCHIVE_DIRNAME,
    BRIEF_SUMMARY_MAX,
    INDEX_FILENAME,
    LEARNING_FOLDER,
    NEXT_SEQ_FILENAME,
    TYPE_FOLDERS,
    ScribeStore,
    derive_brief_summary,
    reset_layout_cache,
)


class TestEnsureLayout(unittest.TestCase):
    """AC: Given a fresh state_dir, ensure_layout creates all folders."""

    def test_creates_all_type_folders_with_index_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "scribe_state"
            year = datetime.now(timezone.utc).year
            ScribeStore(state_dir)  # constructing it is what scaffolds the folders
            all_folders = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]
            for folder_name in all_folders:
                folder = state_dir / folder_name
                self.assertTrue(folder.is_dir(), f"{folder_name} should exist")
                # Year subfolder is the only active layout
                year_dir = folder / str(year)
                self.assertTrue(year_dir.is_dir(), f"{folder_name}/{year} should exist")
                year_idx = year_dir / INDEX_FILENAME
                self.assertTrue(year_idx.exists(), f"{folder_name}/{year}/index.jsonl should exist")
                seq_path = year_dir / NEXT_SEQ_FILENAME
                self.assertTrue(seq_path.exists(), f"{folder_name}/{year}/next_seq should exist")
                self.assertEqual(seq_path.read_text(encoding="utf-8").strip(), "1")
                year_archive = year_dir / ARCHIVE_DIRNAME
                self.assertTrue(year_archive.is_dir(), f"{folder_name}/{year}/archive should exist")
                # Flat layout must NOT be created
                self.assertFalse(
                    (folder / INDEX_FILENAME).exists(),
                    f"{folder_name}/index.jsonl (flat) should not exist",
                )
                self.assertFalse(
                    (folder / ARCHIVE_DIRNAME).exists(),
                    f"{folder_name}/archive (flat) should not exist",
                )
            # next_id file must NOT be created (legacy removed)
            self.assertFalse((state_dir / "next_id").exists())


class TestLazyLayout(unittest.TestCase):
    """The layout is built when it is missing, and not re-checked after that.

    ScribeStore is constructed on the PreToolUse hot path (the learnings
    inject hook builds one per Edit/Write/Bash), where ~45 mkdir/exists/write
    calls per construction were pure waste (review §3 bug 11).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name).resolve() / "scribe_state"
        reset_layout_cache()

    def tearDown(self):
        self._tmp.cleanup()
        reset_layout_cache()

    def test_a_fresh_dir_still_gets_the_full_layout(self):
        ScribeStore(self.state_dir)
        year = str(datetime.now(timezone.utc).year)
        for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
            self.assertTrue((self.state_dir / folder_name / year / ARCHIVE_DIRNAME).is_dir())

    def test_second_construction_does_not_rebuild(self):
        ScribeStore(self.state_dir)
        with unittest.mock.patch.object(ScribeStore, "ensure_layout") as rebuild:
            ScribeStore(self.state_dir)
        rebuild.assert_not_called()

    def test_a_missing_folder_is_rebuilt(self):
        ScribeStore(self.state_dir)
        reset_layout_cache()  # a later process, not this one's cache
        shutil.rmtree(self.state_dir / "todos")
        ScribeStore(self.state_dir)
        year = str(datetime.now(timezone.utc).year)
        self.assertTrue((self.state_dir / "todos" / year / ARCHIVE_DIRNAME).is_dir())

    def test_a_string_state_dir_is_accepted(self):
        store = ScribeStore(str(self.state_dir))
        self.assertIsInstance(store.state_dir, Path)


class TestWriteOrder(unittest.TestCase):
    """A body is written before its index row (review §3 bug 6).

    The old order left, on a crash between the two, an index row with no
    body — which `repair` resolves by deleting the row. The new order leaves
    a body with no row, which `repair` rebuilds.
    """

    def test_add_note_writes_the_body_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            order = []
            real_body = ScribeStore._write_note_file
            real_index = ScribeStore._append_index

            with (
                unittest.mock.patch.object(
                    ScribeStore,
                    "_write_note_file",
                    side_effect=lambda *a: (order.append("body"), real_body(*a))[1],
                ),
                unittest.mock.patch.object(
                    ScribeStore,
                    "_append_index",
                    side_effect=lambda *a: (order.append("index"), real_index(*a))[1],
                ),
            ):
                store.add_note("todo", "body first", "s1")
                store.add_learning("body first", "s1")

            self.assertEqual(order, ["body", "index", "body", "index"])


class TestAddNote(unittest.TestCase):
    """AC: add_note increments next_seq, writes index entry and .md file."""

    def test_add_first_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            result = store.add_note("todo", "fix bug", "abc123")
            self.assertEqual(result["seq"], 1)
            self.assertEqual(result["year"], year)
            self.assertEqual(result["display_id"], f"T-{year}-1")
            self.assertEqual(result["type"], "todo")
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["session"], "abc123")
            self.assertTrue(result["has_body"])
            # Check year-dir index file
            idx_path = Path(tmp) / "todos" / str(year) / INDEX_FILENAME
            lines = [ln for ln in idx_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["seq"], 1)
            # Check .md file in year dir
            md_path = Path(tmp) / "todos" / str(year) / "1.md"
            self.assertEqual(md_path.read_text(encoding="utf-8"), "fix bug")
            # next_seq should now be 2
            seq_path = Path(tmp) / "todos" / str(year) / NEXT_SEQ_FILENAME
            self.assertEqual(seq_path.read_text(encoding="utf-8").strip(), "2")

    def test_add_multiple_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            n1 = store.add_note("todo", "first", "s1")
            n2 = store.add_note("decision", "second", "s1")
            n3 = store.add_note("todo", "third", "s1")
            # Each type has its own per-(type,year) counter
            self.assertEqual(n1["seq"], 1)
            self.assertEqual(n2["seq"], 1)  # decision counter starts at 1
            self.assertEqual(n3["seq"], 2)  # todo counter increments to 2


class TestGetNote(unittest.TestCase):
    """AC: get_note returns index metadata and .md content."""

    def test_get_existing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "fix bug", "s1")
            result = store.get_note(added["type"], added["year"], added["seq"])
            self.assertIsNotNone(result)
            self.assertEqual(result["seq"], 1)
            self.assertEqual(result["content"], "fix bug")

    def test_get_nonexistent_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            self.assertIsNone(store.get_note("todo", year, 999))

    def test_get_note_missing_body(self):
        """Index entry exists but .md file is gone -> content=None, warning."""
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "content here", "s1")
            # Delete the .md file
            md = Path(tmp) / "todos" / str(year) / "1.md"
            md.unlink()
            result = store.get_note(added["type"], added["year"], added["seq"])
            self.assertIsNone(result["content"])
            self.assertEqual(result.get("_warning"), "body_file_missing")


class TestListNotes(unittest.TestCase):
    """AC: list_notes returns all notes sorted by timestamp descending."""

    def test_list_all_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "a", "s1")
            store.add_note("decision", "b", "s1")
            store.add_note("blocker", "c", "s1")
            results = store.list_notes()
            self.assertEqual(len(results), 3)
            # Sorted by timestamp desc — last added is first
            types = [r["type"] for r in results]
            self.assertEqual(types, ["blocker", "decision", "todo"])

    def test_list_filtered_by_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "a", "s1")
            store.add_note("decision", "b", "s1")
            results = store.list_notes(note_type="todo")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["type"], "todo")

    def test_list_with_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "fix the bug", "s1")
            store.add_note("todo", "add feature", "s1")
            results = store.list_notes(search="bug")
            self.assertEqual(len(results), 1)
            self.assertIn("bug", results[0]["summary"])


class TestArchiveNote(unittest.TestCase):
    """AC: archive_note moves index entry and .md to year archive/."""

    def test_archive_moves_entry_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "to archive", "s1")
            result = store.archive_note(added["type"], added["year"], added["seq"])
            self.assertIsNotNone(result)
            # An active note preserves its 'active' status when archived —
            # archived-ness is indicated by folder location and archived_at.
            self.assertEqual(result["status"], "active")
            self.assertIn("archived_at", result)
            # Active year index should be empty
            active = store._read_index(Path(tmp) / "todos" / str(year))
            self.assertEqual(len(active), 0)
            # Archive index under year dir should have the entry
            archived = store._read_index(Path(tmp) / "todos" / str(year) / ARCHIVE_DIRNAME)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]["seq"], 1)
            # .md moved to year archive
            self.assertFalse((Path(tmp) / "todos" / str(year) / "1.md").exists())
            self.assertTrue((Path(tmp) / "todos" / str(year) / "archive" / "1.md").exists())

    def test_archive_preserves_done_status(self):
        """Archiving a done note must preserve status='done' — the original
        bug clobbered it to 'archived', erasing completion history."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "finished work", "s1")
            store.update_note(added["type"], added["year"], added["seq"], status="done")
            result = store.archive_note(added["type"], added["year"], added["seq"])
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "done")
            self.assertIn("archived_at", result)
            # And the archive index reflects the same.
            listed = store.list_notes(note_type="todo", status="archived")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["status"], "done")

    def test_get_archived_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "archive me", "s1")
            store.archive_note(added["type"], added["year"], added["seq"])
            result = store.get_note(added["type"], added["year"], added["seq"])
            self.assertIsNotNone(result)
            # get_note returns the real status plus _from_archive flag
            self.assertEqual(result["status"], "active")
            self.assertTrue(result.get("_from_archive"))
            self.assertEqual(result["content"], "archive me")


class TestConcurrency(unittest.TestCase):
    """AC: concurrent add_note calls get unique seqs without corruption."""

    def test_concurrent_adds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            results = []
            errors = []

            def add(i):
                try:
                    r = store.add_note("todo", f"note {i}", "concurrent")
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=add, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"Errors: {errors}")
            self.assertEqual(len(results), 10)
            seqs = {r["seq"] for r in results}
            self.assertEqual(len(seqs), 10, "All seqs should be unique")


class TestNextSeqRebuild(unittest.TestCase):
    """AC: next_seq file deleted -> rebuild from max seq in year index."""

    def test_rebuild_after_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "a", "s1")  # seq=1
            store.add_note("todo", "b", "s1")  # seq=2
            # Delete next_seq for the todo year dir
            seq_path = Path(tmp) / "todos" / str(year) / NEXT_SEQ_FILENAME
            seq_path.unlink()
            # Next add should rebuild and get seq=3
            result = store.add_note("todo", "c", "s1")
            self.assertEqual(result["seq"], 3)


class TestMalformedIndex(unittest.TestCase):
    """AC: malformed line in index.jsonl is skipped with warning."""

    def test_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "good note", "s1")
            # Inject a bad line into the year index
            idx = Path(tmp) / "todos" / str(year) / INDEX_FILENAME
            content = idx.read_text(encoding="utf-8")
            idx.write_text("NOT VALID JSON\n" + content, encoding="utf-8")
            # list_notes should still work, returning only the valid entry
            results = store.list_notes(note_type="todo")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["seq"], 1)

    def test_rebuild_raises_on_malformed_index(self):
        """Rebuild must NOT silently skip bad lines — it would undercount seq.

        If next_seq is missing and the index has a malformed line covering the
        real max seq, a skipping rebuild would reset the counter below an
        already-used ID and the next write would collide.
        """
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "a", "s1")  # seq=1
            store.add_note("todo", "b", "s1")  # seq=2
            year_dir = Path(tmp) / "todos" / str(year)
            idx = year_dir / INDEX_FILENAME
            # Corrupt the line for seq=2 so a skipping rebuild would max at 1
            lines = idx.read_text(encoding="utf-8").splitlines()
            lines[-1] = "NOT VALID JSON"
            idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # Drop next_seq so the next write triggers a rebuild
            (year_dir / NEXT_SEQ_FILENAME).unlink()
            with self.assertRaises(RuntimeError) as ctx:
                store.add_note("todo", "c", "s1")
            self.assertIn("Malformed index entry", str(ctx.exception))


class TestUpdateNote(unittest.TestCase):
    def test_update_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "original", "s1")
            result = store.update_note(
                added["type"], added["year"], added["seq"], summary="updated summary"
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["summary"], "updated summary")

    def test_update_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "original", "s1")
            store.update_note(added["type"], added["year"], added["seq"], content="new body")
            md = Path(tmp) / "todos" / str(year) / "1.md"
            self.assertEqual(md.read_text(encoding="utf-8"), "new body")


class TestLearnings(unittest.TestCase):
    def test_add_and_list_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            l1 = store.add_learning("learned thing 1", "s1")
            store.add_learning("learned thing 2", "s1")
            self.assertEqual(l1["type"], "learning")
            results = store.list_learnings()
            self.assertEqual(len(results), 2)

    def test_get_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning("my learning", "s1")
            result = store.get_learning(added["year"], added["seq"])
            self.assertEqual(result["content"], "my learning")

    def test_remove_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning("remove me", "s1")
            removed = store.remove_learning(added["year"], added["seq"])
            self.assertIsNotNone(removed)
            self.assertIsNone(store.get_learning(added["year"], added["seq"]))

    def test_search_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_learning("encoding workaround", "s1")
            store.add_learning("api quirk", "s1")
            results = store.list_learnings(search="encoding")
            self.assertEqual(len(results), 1)


class TestLearningFrontmatter(unittest.TestCase):
    """Frontmatter round-trip, filters, archive, and legacy tolerance."""

    def test_add_learning_writes_frontmatter_to_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_learning(
                "body text here",
                "s1",
                tags=["windows", "encoding"],
                areas=["core/**", "scripts/*"],
                supersedes="L-2026-5",
            )
            md = Path(tmp) / LEARNING_FOLDER / str(year) / f"{added['seq']}.md"
            raw = md.read_text(encoding="utf-8")
            self.assertTrue(raw.startswith("---\n"))
            self.assertIn("tags: [windows, encoding]", raw)
            self.assertIn("areas: [core/**, scripts/*]", raw)
            self.assertIn("supersedes: L-2026-5", raw)
            self.assertTrue(raw.rstrip().endswith("body text here"))

    def test_add_learning_no_frontmatter_when_fields_empty(self):
        """Legacy-compatible: no tags/areas/supersedes → .md stays frontmatter-free."""
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_learning("plain body", "s1")
            md = Path(tmp) / LEARNING_FOLDER / str(year) / f"{added['seq']}.md"
            self.assertEqual(md.read_text(encoding="utf-8"), "plain body")

    def test_get_learning_returns_frontmatter_fields_and_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning(
                "payload",
                "s1",
                tags=["a", "b"],
                areas=["core/**"],
            )
            got = store.get_learning(added["year"], added["seq"])
            self.assertEqual(got["content"], "payload")
            self.assertEqual(got["tags"], ["a", "b"])
            self.assertEqual(got["areas"], ["core/**"])

    def test_get_learning_tolerates_missing_frontmatter(self):
        """Legacy .md files (no frontmatter) still round-trip."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning("no-fm body", "s1")
            got = store.get_learning(added["year"], added["seq"])
            self.assertEqual(got["content"], "no-fm body")
            self.assertEqual(got.get("tags", []), [])

    def test_list_learnings_filters_by_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_learning("a", "s1", tags=["windows", "gui"])
            store.add_learning("b", "s1", tags=["subprocess"])
            store.add_learning("c", "s1")
            win = store.list_learnings(tag="windows")
            self.assertEqual([e["summary"] for e in win], ["a"])
            subproc = store.list_learnings(tag="subprocess")
            self.assertEqual([e["summary"] for e in subproc], ["b"])

    def test_list_learnings_filters_by_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_learning("a", "s1", areas=["gui/**"])
            store.add_learning("b", "s1", areas=["scribe/*"])
            results = store.list_learnings(area="gui/**")
            self.assertEqual([e["summary"] for e in results], ["a"])

    def test_archive_learning_moves_index_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_learning("archive me", "s1", tags=["x"])
            archived = store.archive_learning(added["year"], added["seq"])
            self.assertIsNotNone(archived)
            self.assertIn("archived_at", archived)
            # Active index empty
            active = store.list_learnings()
            self.assertEqual(active, [])
            # Archive index has it
            from_archive = store.list_learnings(status="archived")
            self.assertEqual(len(from_archive), 1)
            self.assertTrue(from_archive[0].get("_from_archive"))
            # .md moved
            year_dir = Path(tmp) / LEARNING_FOLDER / str(year)
            self.assertFalse((year_dir / f"{added['seq']}.md").exists())
            self.assertTrue((year_dir / ARCHIVE_DIRNAME / f"{added['seq']}.md").exists())

    def test_get_learning_falls_back_to_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning("findable after archive", "s1", tags=["t"])
            store.archive_learning(added["year"], added["seq"])
            got = store.get_learning(added["year"], added["seq"])
            self.assertIsNotNone(got)
            self.assertTrue(got.get("_from_archive"))
            self.assertEqual(got["content"], "findable after archive")
            self.assertEqual(got["tags"], ["t"])

    def test_list_learnings_status_all_unions_active_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_learning("alive", "s1")
            b = store.add_learning("dead", "s1")
            store.archive_learning(b["year"], b["seq"])
            everything = store.list_learnings(status="all")
            summaries = sorted(e["summary"] for e in everything)
            self.assertEqual(summaries, ["alive", "dead"])


class TestEmptyContent(unittest.TestCase):
    def test_empty_content_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            result = store.add_note("todo", "", "s1")
            self.assertFalse(result["has_body"])
            md = Path(tmp) / "todos" / str(year) / "1.md"
            self.assertEqual(md.read_text(encoding="utf-8"), "")


class TestDeriveBriefSummary(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(derive_brief_summary(""), "")
        self.assertEqual(derive_brief_summary("   \n  "), "")

    def test_markdown_header_takes_first_line_without_hashes(self):
        content = "## Goal\n\nRewrite the auth middleware.\n\nSteps follow."
        self.assertEqual(derive_brief_summary(content), "Goal")

    def test_first_sentence_preferred(self):
        content = "Fix the crash. Then add the feature. Then celebrate."
        self.assertEqual(derive_brief_summary(content), "Fix the crash.")

    def test_mid_word_cut_trims_to_last_space_with_ellipsis(self):
        # One token ("supercalifragilisticexpialidocious") kept whole-or-omitted
        # so we can prove the truncator never splits a word mid-letters.
        token = "supercalifragilisticexpialidocious"  # apiary:allow-secret
        content = " ".join([token] * 10) + " end-of-sentence-without-period"
        result = derive_brief_summary(content)
        self.assertLessEqual(len(result), BRIEF_SUMMARY_MAX + 1)
        self.assertTrue(result.endswith("…"))
        # Everything before the ellipsis (excluding trailing space) must be a
        # sequence of complete tokens — no partial token clipped by the cap.
        body = result[:-1].rstrip()
        for tok in body.split():
            self.assertTrue(
                tok == token or tok == "end-of-sentence-without-period",
                f"unexpected partial token in truncated result: {tok!r}",
            )

    def test_short_content_returned_verbatim(self):
        self.assertEqual(derive_brief_summary("fix the bug"), "fix the bug")

    def test_real_note_that_hit_the_permissi_bug(self):
        content = (
            "GUI V1 open kinks (post-detector session). Items 1 (intermittent "
            "Enter bug) and 2 (untested permission-prompt auto-expand) from the "
            "original list are obsolete."
        )
        result = derive_brief_summary(content)
        self.assertTrue(result.endswith("."), f"expected sentence-end, got {result!r}")
        self.assertIn("GUI V1 open kinks", result)
        self.assertNotIn("permissi ", result + " ")  # never leave a mid-word fragment

    def test_newlines_collapsed_to_spaces(self):
        content = "first line\nsecond line. third."
        result = derive_brief_summary(content)
        self.assertNotIn("\n", result)
        self.assertEqual(result, "first line second line.")

    def test_header_colon_used_when_no_sentence_end_and_no_paren(self):
        # Colon fallback is used only when no sentence-end and no closing
        # paren in the meaningful range.
        content = "Tier 2 partial deferral details: a, b, c, and more continuing on"
        self.assertEqual(
            derive_brief_summary(content),
            "Tier 2 partial deferral details:",
        )

    def test_colon_ignored_when_stuck_to_following_char(self):
        # "http://" / "C:\path" — colon followed by non-space should NOT trigger.
        content = (
            "See http://example.com/docs for context and related work stretching on and on without a sentence end right here either in any form oh dear"
            * 2
        )
        result = derive_brief_summary(content)
        self.assertFalse(result.endswith(":"))

    def test_closing_paren_used_for_informative_parenthetical(self):
        # L-2026-17 shape: "... (not just tool calls), continues on and on..."
        content = (
            "UserPromptSubmit hooks fire on every user message (not just tool calls), "
            "making them ideal for injecting startup context throughout the session"
        )
        self.assertEqual(
            derive_brief_summary(content),
            "UserPromptSubmit hooks fire on every user message (not just tool calls)",
        )

    def test_closing_paren_keeps_date_in_header(self):
        # D-2026-13 shape: "<header> (date): <long list>"
        content = (
            "Tier 2 partial deferral (decided 2026-04-06): L-2026-15 (budgeter usage), "
            "L-2026-3 (budgeter rule retune), and more"
        )
        self.assertEqual(
            derive_brief_summary(content),
            "Tier 2 partial deferral (decided 2026-04-06)",
        )

    def test_closing_paren_skipped_when_too_early(self):
        # "(WIP) Do thing" — paren at pos 4 is too early to be a meaningful cut.
        content = "(WIP) Do thing X and Y and Z"
        self.assertEqual(derive_brief_summary(content), "(WIP) Do thing X and Y and Z")

    def test_em_dash_cut_before_elaboration(self):
        # Long run-on: main clause — elaboration.
        content = (
            "session-history.json stores absolute transcript_path entries — "
            "any project folder rename must update these paths too or the "
            "tail breaks on stale state"
        )
        self.assertEqual(
            derive_brief_summary(content),
            "session-history.json stores absolute transcript_path entries",
        )

    def test_deep_comma_cut_as_last_resort(self):
        # No sentence, paren, colon, or dash — fall back to last comma >= 60.
        content = (
            "Hooks that scan Claude Code session transcripts must walk structured "
            "tool_use blocks, carrying the assistant context forward for downstream tools"
        )
        result = derive_brief_summary(content)
        self.assertFalse(result.endswith("…"))
        self.assertTrue(result.endswith("blocks"), f"expected comma cut, got {result!r}")


class TestBriefSummaryOnAdd(unittest.TestCase):
    def test_auto_derived_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("todo", "First sentence. Second sentence.", "s1")
            self.assertEqual(entry["brief_summary"], "First sentence.")

    def test_explicit_override_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note(
                "todo", "some content here", "s1", brief_summary="hand-written title"
            )
            self.assertEqual(entry["brief_summary"], "hand-written title")

    def test_explicit_brief_capped_at_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            long_brief = "x" * (BRIEF_SUMMARY_MAX + 50)
            entry = store.add_note("todo", "body", "s1", brief_summary=long_brief)
            self.assertEqual(len(entry["brief_summary"]), BRIEF_SUMMARY_MAX)

    def test_learning_gets_brief_summary_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_learning("Fix: pass --foo. Reason: obscure flag.", "s1")
            self.assertEqual(entry["brief_summary"], "Fix: pass --foo.")

    def test_persisted_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note("todo", "One. Two. Three.", "s1")
            idx = Path(tmp) / "todos" / str(year) / INDEX_FILENAME
            entry = json.loads(idx.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["brief_summary"], "One.")


class TestBriefSummaryOnUpdate(unittest.TestCase):
    def test_redetivred_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "First version.", "s1")
            updated = store.update_note(
                added["type"],
                added["year"],
                added["seq"],
                content="Rewritten version. More detail.",
            )
            self.assertEqual(updated["brief_summary"], "Rewritten version.")

    def test_explicit_brief_overrides_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note("todo", "First.", "s1")
            updated = store.update_note(
                added["type"],
                added["year"],
                added["seq"],
                content="Rewritten.",
                brief_summary="hand-picked title",
            )
            self.assertEqual(updated["brief_summary"], "hand-picked title")


if __name__ == "__main__":
    unittest.main()
