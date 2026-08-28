#!/usr/bin/env python3
"""Tests for docs/hooks/remind_standards.py.

The hook was silently dead outside main-apiary (T-2026-282): every path was
resolved against apiary's own checkout, so `relative_to` raised and the hook
returned None for every file in every other bootstrapped repo. Inside
main-apiary it was worse than dead — `known_dirs` listed three tool
directories, so writing `runner/executor.py` or `gui/app.py` was classified as
starting a *new tool*.

These tests pin both halves: classification is relative to the session's repo,
and "new tool" means the directory really has no other Python in it.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "apiary_remind_standards", REPO_ROOT / "docs" / "hooks" / "remind_standards.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "docs" / "standards").mkdir(parents=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "existing.py").write_text("x = 1", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _classify(self, rel: str) -> str | None:
        return rs._classify_file(str(self.repo / rel), self.repo)

    def test_a_doc_is_a_doc(self):
        self.assertEqual(self._classify("docs/standards/x.md"), "doc")

    def test_python_in_an_established_dir_is_code(self):
        self.assertEqual(self._classify("src/new_module.py"), "code")

    def test_python_in_a_dir_with_no_other_python_is_a_new_tool(self):
        (self.repo / "brandnew").mkdir()
        self.assertEqual(self._classify("brandnew/cli.py"), "tool")

    def test_python_in_a_dir_that_does_not_exist_yet_is_a_new_tool(self):
        self.assertEqual(self._classify("notyet/cli.py"), "tool")

    def test_a_top_level_python_file_is_code_not_a_tool(self):
        self.assertEqual(self._classify("setup_thing.py"), "code")

    def test_a_file_outside_the_repo_is_not_classified(self):
        other = Path(tempfile.gettempdir()).resolve() / "elsewhere" / "x.py"
        self.assertIsNone(rs._classify_file(str(other), self.repo))

    def test_a_non_python_non_doc_file_is_not_classified(self):
        self.assertIsNone(self._classify("src/data.json"))


class RepoResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "tool").mkdir()
        (self.repo / "tool" / "a.py").write_text("x = 1", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self._saved = {k: os.environ.get(k)
                       for k in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO")}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_claude_project_dir_wins(self):
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.assertEqual(rs._session_repo({}), self.repo)

    def test_launcher_env_is_the_fallback(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        self.assertEqual(rs._session_repo({}), self.repo)

    def test_payload_cwd_is_used_when_no_env_is_set(self):
        self.assertEqual(rs._session_repo({"cwd": str(self.repo)}), self.repo)

    def test_no_signal_at_all_falls_back_to_main_apiary(self):
        self.assertEqual(rs._session_repo({}), rs.REPO_ROOT)


class HookBehaviourTests(unittest.TestCase):
    """The end-to-end path the dispatcher takes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "tool").mkdir(parents=True)
        (self.repo / "tool" / "existing.py").write_text("x = 1", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self._saved = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.addCleanup(self._restore)
        self._reminded: set[tuple[str, str]] = set()
        self._real_seen = rs._already_reminded
        self._real_mark = rs._mark_reminded
        rs._already_reminded = lambda sid, key: (sid, key) in self._reminded
        rs._mark_reminded = lambda sid, key: self._reminded.add((sid, key))
        self.addCleanup(self._unpatch)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._saved

    def _unpatch(self):
        rs._already_reminded = self._real_seen
        rs._mark_reminded = self._real_mark

    def _payload(self, rel: str) -> dict:
        return {"tool_name": "Write", "session_id": "abcdef01-2345-6789-abcd-ef0123456789",
                "tool_input": {"file_path": str(self.repo / rel)}}

    def test_a_write_in_another_repo_now_produces_a_reminder(self):
        result = rs.run(self._payload("tool/new.py"))
        self.assertIsNotNone(result, "the hook is dead outside main-apiary again")
        self.assertIn("code-style.md", result.context)

    def test_the_reminder_fires_once_per_category(self):
        self.assertIsNotNone(rs.run(self._payload("tool/new.py")))
        self.assertIsNone(rs.run(self._payload("tool/another.py")))

    def test_a_non_write_tool_is_ignored(self):
        payload = self._payload("tool/new.py") | {"tool_name": "Read"}
        self.assertIsNone(rs.run(payload))

    def test_the_named_standard_falls_back_to_main_apiary_when_absent(self):
        named = rs._standard_for("code", self.repo)
        self.assertTrue(named.endswith("docs/standards/code-style.md"))
        self.assertNotEqual(named, "docs/standards/code-style.md")

    def test_the_named_standard_is_repo_relative_when_present(self):
        (self.repo / "docs" / "standards").mkdir(parents=True)
        (self.repo / "docs" / "standards" / "code-style.md").write_text(
            "x", encoding="utf-8")
        self.assertEqual(rs._standard_for("code", self.repo),
                         "docs/standards/code-style.md")


if __name__ == "__main__":
    unittest.main()
