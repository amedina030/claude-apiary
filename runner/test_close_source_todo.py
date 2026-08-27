#!/usr/bin/env python3
"""Tests for runner/close_source_todo.py — the post-merge auto-close path.

Stdlib unittest + real temp git repos. We build a repo with a runner
branch, merge it into master with --no-ff (so there's a real merge
commit), populate a temp run_history.jsonl with a source field, and
exercise the module's functions against that state.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import close_source_todo


def _git(repo: Path, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        check=check,
    )


class TestUuidsFromMerge(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        os.chdir(self.repo)
        _git(self.repo, "init", "--initial-branch=master")
        _git(self.repo, "config", "user.email", "t@e.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "initial")

        # Point the module at this repo.
        self._module_script_dir_patch = mock.patch.object(
            close_source_todo, "SCRIPT_DIR", self.repo / "runner",
        )
        self._module_script_dir_patch.start()
        (self.repo / "runner").mkdir()
        self._history_path = self.repo / "runner" / "run_history.jsonl"
        self._history_patch = mock.patch.object(
            close_source_todo, "RUN_HISTORY_FILE", self._history_path,
        )
        self._history_patch.start()

    def tearDown(self):
        self._history_patch.stop()
        self._module_script_dir_patch.stop()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _make_runner_branch(self, uuid: str):
        _git(self.repo, "checkout", "-b", f"runner/{uuid}")
        (self.repo / f"{uuid}.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", f"{uuid}.txt")
        _git(self.repo, "commit", "-m", f"runner/{uuid} step 1: do the thing")
        _git(self.repo, "checkout", "master")

    def test_uuids_from_non_merge_head_is_empty(self):
        # HEAD is a single-parent commit — not a merge. Return [].
        merge_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(close_source_todo.uuids_from_merge(merge_sha), [])

    def test_uuid_extracted_from_merge_commit(self):
        uuid = "abc12345-dead-beef-cafe-0123456789ab"
        self._make_runner_branch(uuid)
        _git(self.repo, "merge", "--no-ff", f"runner/{uuid}",
             "-m", f"Merge branch 'runner/{uuid}'")
        merge_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        uuids = close_source_todo.uuids_from_merge(merge_sha)
        self.assertEqual(uuids, [uuid])

    def test_merge_with_no_runner_subject_returns_empty(self):
        _git(self.repo, "checkout", "-b", "feature/plain")
        (self.repo / "a.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-m", "unrelated feature work")
        _git(self.repo, "checkout", "master")
        _git(self.repo, "merge", "--no-ff", "feature/plain",
             "-m", "Merge branch 'feature/plain'")
        merge_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(close_source_todo.uuids_from_merge(merge_sha), [])


class TestSourceForUuid(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self._tmp.name) / "run_history.jsonl"
        self._patch = mock.patch.object(
            close_source_todo, "RUN_HISTORY_FILE", self.history_path,
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, entries):
        self.history_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )

    def test_missing_file_returns_empty(self):
        self.assertEqual(close_source_todo.source_for_uuid("xyz"), "")

    def test_matching_entry_returns_source(self):
        self._write([
            {"uuid": "abc", "source": "T-2026-121"},
        ])
        self.assertEqual(
            close_source_todo.source_for_uuid("abc"), "T-2026-121",
        )

    def test_no_matching_entry_returns_empty(self):
        self._write([
            {"uuid": "other", "source": "T-2026-1"},
        ])
        self.assertEqual(close_source_todo.source_for_uuid("abc"), "")

    def test_last_matching_entry_wins(self):
        # Same uuid appears twice (e.g. a resume) — take the most recent
        # source (newest last in JSONL).
        self._write([
            {"uuid": "abc", "source": "T-2026-1"},
            {"uuid": "abc", "source": "T-2026-121"},
        ])
        self.assertEqual(
            close_source_todo.source_for_uuid("abc"), "T-2026-121",
        )

    def test_malformed_line_skipped(self):
        self.history_path.write_text(
            'not-json\n'
            '{"uuid": "abc", "source": "T-2026-121"}\n',
            encoding="utf-8",
        )
        self.assertEqual(
            close_source_todo.source_for_uuid("abc"), "T-2026-121",
        )


class TestTodoIdFilter(unittest.TestCase):
    """Only T-YYYY-N patterns trigger auto-close. H-/C-/W- are ignored so
    we don't accidentally mark handoffs or wishlist items 'done'."""

    def test_todo_id_matches(self):
        self.assertTrue(close_source_todo._TODO_ID_RE.match("T-2026-121"))
        self.assertTrue(close_source_todo._TODO_ID_RE.match("T-2026-1"))

    def test_non_todo_ids_rejected(self):
        for s in ("H-2026-145", "C-2026-27", "W-2026-10", "", "garbage"):
            self.assertIsNone(
                close_source_todo._TODO_ID_RE.match(s), s,
            )


if __name__ == "__main__":
    unittest.main()
