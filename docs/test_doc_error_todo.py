#!/usr/bin/env python3
"""Tests for the doc-shaped-failure → scribe-todo path (review §5a-D.4).

The behaviour lives in ``core/hooks/context_rule_error_reminder.py`` but it is
the documentation framework's feature: when a documented command fails because
the doc is wrong, the doc gets a todo naming the line. The hook's own generic
failure detection is tested in ``core/hooks/test_context_rule_error_reminder.py``;
this file covers only the doc half, and lives beside the rest of the framework
it belongs to.

Every case drives the hook with a fake ``tool_response``. The scribe call is
exercised against a throwaway repo with a stub launcher, so no note is ever
written into a real store.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "core" / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import context_rule_error_reminder as hook  # noqa: E402

SESSION_ID = "abcdef01-2345-6789-abcd-ef0123456789"


def payload(command: str, stderr: str, **extra) -> dict:
    return {
        "tool_name": "Bash",
        "session_id": SESSION_ID,
        "tool_input": {"command": command},
        "tool_response": {"exit_code": 2, "stderr": stderr},
        **extra,
    }


class TokenExtractionTests(unittest.TestCase):
    def test_unrecognized_argument(self):
        self.assertEqual(
            hook.offending_token("usage: notes.py\nnotes.py: error: unrecognized arguments: --nope"),
            "--nope")

    def test_invalid_choice(self):
        self.assertEqual(
            hook.offending_token(
                "error: argument command: invalid choice: 'learning' (choose from 'add')"),
            "learning")

    def test_missing_script(self):
        self.assertEqual(
            hook.offending_token("python: can't open file 'runner/intake/x.json': [Errno 2]"),
            "runner/intake/x.json")

    def test_a_plain_traceback_is_not_doc_shaped(self):
        self.assertIsNone(hook.offending_token(
            "Traceback (most recent call last):\n  ZeroDivisionError"))

    def test_empty_text(self):
        self.assertIsNone(hook.offending_token(""))


class DocLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "docs" / "reference").mkdir(parents=True)
        (self.repo / "docs" / "reference" / "cli-tools.md").write_text(
            "# CLI\n\nline two\n\n| `--nope` | does a thing |\n", encoding="utf-8")
        (self.repo / "docs" / "review").mkdir(parents=True)
        (self.repo / "docs" / "review" / "old.md").write_text(
            "the review mentions --snapshot-only\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_it_returns_doc_and_line(self):
        self.assertEqual(hook.find_doc_line("--nope", self.repo),
                         "docs/reference/cli-tools.md:5")

    def test_an_undocumented_token_returns_none(self):
        self.assertIsNone(hook.find_doc_line("--never-written-down", self.repo))

    def test_the_review_snapshots_are_never_blamed(self):
        self.assertIsNone(hook.find_doc_line("--snapshot-only", self.repo))

    def test_matching_is_literal_not_prefix(self):
        self.assertIsNone(hook.find_doc_line("--no", self.repo))


class DocGapTests(unittest.TestCase):
    """`doc_gap` only fires for a failure this command actually caused."""

    def setUp(self):
        self._saved = os.environ.get("CLAUDE_PROJECT_DIR")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "d.md").write_text("run it with --nope\n", encoding="utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._saved
        self._tmp.cleanup()

    def test_a_documented_flag_that_argparse_lost_is_a_gap(self):
        gap = hook.doc_gap(payload("python t.py --nope",
                                   "error: unrecognized arguments: --nope"))
        self.assertEqual(gap, ("--nope", "docs/d.md:1"))

    def test_a_token_the_command_never_passed_is_not_this_commands_gap(self):
        # A nested process complained about something this invocation did not
        # use — blaming the doc for it would be wrong.
        self.assertIsNone(hook.doc_gap(
            payload("python t.py", "error: unrecognized arguments: --nope")))

    def test_an_undocumented_typo_files_nothing(self):
        self.assertIsNone(hook.doc_gap(
            payload("python t.py --typpo", "error: unrecognized arguments: --typpo")))

    def test_no_command_means_no_gap(self):
        p = payload("python t.py --nope", "error: unrecognized arguments: --nope")
        p["tool_input"] = {}
        self.assertIsNone(hook.doc_gap(p))


class TodoFilingTests(unittest.TestCase):
    """The scribe call goes through the repo's launcher, once per session."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        launcher_dir = self.repo / ".claude" / "apiary"
        launcher_dir.mkdir(parents=True)
        self.record = self.repo / "calls.jsonl"
        # A stub launcher: records argv instead of touching a real scribe store.
        (launcher_dir / "launch.py").write_text(
            "import json, sys, pathlib\n"
            f"pathlib.Path(r'{self.record}').open('a', encoding='utf-8')"
            ".write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _calls(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(ln) for ln in
                self.record.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_it_invokes_notes_add_through_the_launcher(self):
        self.assertTrue(hook.file_doc_todo(
            self.repo, "--nope", "docs/d.md:1", "python t.py --nope"))
        calls = self._calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["scribe/notes.py", "add", "--type", "todo"])

    def test_the_todo_names_the_doc_the_line_and_the_command(self):
        hook.file_doc_todo(self.repo, "--nope", "docs/d.md:7", "python t.py --nope")
        content = self._calls()[0][self._calls()[0].index("--content") + 1]
        self.assertIn("docs/d.md:7", content)
        self.assertIn("--nope", content)
        self.assertIn("python t.py --nope", content)

    def test_it_carries_a_unique_tag_so_reruns_do_not_pile_up(self):
        hook.file_doc_todo(self.repo, "--nope", "docs/d.md:1", "python t.py --nope")
        argv = self._calls()[0]
        self.assertIn("--unique-tag", argv)
        self.assertTrue(argv[argv.index("--unique-tag") + 1].startswith("doc-drift:"))

    def test_an_unbootstrapped_repo_files_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(hook.file_doc_todo(
                Path(td), "--nope", "docs/d.md:1", "python t.py --nope"))


class OncePerSessionTests(unittest.TestCase):
    def setUp(self):
        self._filed: set[str] = set()
        self._real = hook._already_filed
        hook._already_filed = self._fake
        self.addCleanup(lambda: setattr(hook, "_already_filed", self._real))

    def _fake(self, session_id, key):
        seen = key in self._filed
        self._filed.add(key)
        return seen

    def test_the_key_is_the_command_shape_not_its_arguments(self):
        a = hook._command_key('python scribe/notes.py add --content "one"')
        b = hook._command_key('python scribe/notes.py add --content "two"')
        self.assertEqual(a, b)

    def test_different_subcommands_get_different_keys(self):
        self.assertNotEqual(hook._command_key("python scribe/notes.py add"),
                            hook._command_key("python scribe/notes.py learn"))

    def test_the_key_is_filename_safe(self):
        key = hook._command_key("python a/b/c.py sub --x 'q u o t e'")
        self.assertTrue(all(ch.isalnum() or ch in "._-" for ch in key), key)


class EndToEndTests(unittest.TestCase):
    """`run()` still returns the behavioural reminder, plus the doc note."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "d.md").write_text("use --nope\n", encoding="utf-8")
        self._saved = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self._real_filed = hook._already_filed
        self._real_file = hook.file_doc_todo
        self.filed_args = []
        hook._already_filed = lambda sid, key: False
        hook.file_doc_todo = lambda *a: self.filed_args.append(a) or True
        self.addCleanup(self._restore)

    def _restore(self):
        hook._already_filed = self._real_filed
        hook.file_doc_todo = self._real_file
        if self._saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._saved
        self._tmp.cleanup()

    def test_a_doc_shaped_failure_adds_the_doc_note_and_files(self):
        result = hook.run(payload("python t.py --nope",
                                  "error: unrecognized arguments: --nope"))
        self.assertIsNotNone(result)
        self.assertIn("recover_from_trivial_errors", result.context)
        self.assertIn("docs/d.md:1", result.context)
        self.assertEqual(len(self.filed_args), 1)

    def test_an_ordinary_failure_is_unchanged_and_files_nothing(self):
        result = hook.run(payload("python t.py", "Traceback (most recent call last):"))
        self.assertIsNotNone(result)
        self.assertNotIn("[docs]", result.context)
        self.assertEqual(self.filed_args, [])

    def test_a_success_returns_none(self):
        p = payload("python t.py --nope", "")
        p["tool_response"] = {"exit_code": 0, "stdout": "fine"}
        self.assertIsNone(hook.run(p))

    def test_a_second_identical_failure_files_nothing_more(self):
        hook._already_filed = self._real_filed   # use the real once-per-session guard
        seen = {}
        hook._already_filed = lambda sid, key: seen.setdefault(key, False) or bool(
            seen.__setitem__(key, True))
        p = payload("python t.py --nope", "error: unrecognized arguments: --nope")
        hook.run(p)
        hook.run(p)
        self.assertEqual(len(self.filed_args), 1)

    def test_the_hook_never_raises_on_a_malformed_payload(self):
        for bad in ({"tool_name": "Bash", "tool_response": {"exit_code": 1}},
                    {"tool_name": "Bash", "tool_response": "error", "tool_input": None},
                    {"tool_name": "Bash", "tool_response": {"is_error": True},
                     "tool_input": {"command": None}}):
            with self.subTest(payload=bad):
                hook.run(bad)   # must not raise


if __name__ == "__main__":
    unittest.main()
