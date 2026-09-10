"""Tests for prose/hooks/pre_tool_use.py: the once-per-session reminder, advisory findings, the block."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import session as core_session  # noqa: E402
from prose import gate  # noqa: E402
from prose.hooks import pre_tool_use as hook  # noqa: E402

SID = "abcd1234-1111-2222-3333-444444444444"
LAUNCH = 'python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py"'


class IsNoteWriteTest(unittest.TestCase):
    def test_add_and_learn_through_the_launcher(self):
        self.assertTrue(hook.is_note_write(f"{LAUNCH} scribe/notes.py add --type todo --content x"))
        self.assertTrue(hook.is_note_write(f"{LAUNCH} scribe/notes.py learn --content x"))
        self.assertTrue(hook.is_note_write("python D:\\repo\\scribe\\notes.py add --type todo"))
        self.assertTrue(hook.is_note_write(f"cd /x && {LAUNCH} scribe/notes.py --some-flag add"))

    def test_other_verbs_and_commands_do_not_count(self):
        for cmd in (
            f"{LAUNCH} scribe/notes.py list --type todo",
            f"{LAUNCH} scribe/notes.py get T-2026-1",
            "git add -A",
            "grep notes.py core/",
            "",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(hook.is_note_write(cmd))


class HookRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.flags = self.root / "session-tmp"
        self.flags.mkdir()
        env = mock.patch.dict(os.environ, {"APIARY_TARGET_REPO": str(self.repo)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("APIARY_RUNNER_SUBPROCESS", None)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        flag_patch = mock.patch.object(core_session, "session_tmp_dir", lambda: self.flags)
        flag_patch.start()
        self.addCleanup(flag_patch.stop)
        block_patch = mock.patch.object(gate, "blocking_enabled", lambda: False)
        block_patch.start()
        self.addCleanup(block_patch.stop)

    def payload(self, tool_name, **tool_input):
        return {
            "session_id": SID,
            "hook_event_name": "PreToolUse",
            "cwd": str(self.repo),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }

    def write(self, name, content, tool="Write"):
        key = "content" if tool == "Write" else "new_string"
        return hook.run(self.payload(tool, file_path=str(self.repo / name), **{key: content}))

    def test_reminder_once_per_session_on_note_write(self):
        first = hook.run(
            self.payload("Bash", command=f"{LAUNCH} scribe/notes.py add --type todo --content x")
        )
        self.assertIsNotNone(first)
        self.assertIn("[prose] Prose rules", first.context)
        self.assertIn("CandourPhrases [error]", first.context)
        self.assertIn("prose/cli.py check", first.context)
        self.assertIsNone(first.block_reason)
        second = hook.run(
            self.payload("Bash", command=f"{LAUNCH} scribe/notes.py learn --content y")
        )
        self.assertIsNone(second)

    def test_unrelated_bash_is_ignored(self):
        self.assertIsNone(hook.run(self.payload("Bash", command="git status")))

    def test_markdown_write_gets_reminder_then_findings(self):
        result = self.write("docs/a.md", "In summary, it works.\n")
        self.assertIn("[prose] Prose rules", result.context)
        self.assertIn("docs/a.md: 1 finding(s): 1 warning", result.context)
        self.assertIn("SummaryCloser", result.context)
        again = self.write("docs/b.md", "In summary, it works.\n")
        self.assertNotIn("Prose rules", again.context)
        self.assertIn("docs/b.md", again.context)

    def test_clean_markdown_write_after_reminder_is_silent(self):
        self.write("docs/a.md", "Clean.\n")
        self.assertIsNone(self.write("docs/b.md", "Still clean.\n"))

    def test_suggestions_alone_do_not_inject(self):
        self.write("docs/a.md", "Clean.\n")
        self.assertIsNone(self.write("docs/b.md", "We utilize it.\n"))

    def test_non_markdown_and_excluded_paths_are_ignored(self):
        self.assertIsNone(self.write("core/x.py", "To be clear, code.\n"))
        (self.repo / ".repos" / "t").mkdir(parents=True)
        self.assertIsNone(self.write(".repos/t/note.md", "To be clear, excluded.\n"))

    def test_edit_new_string_is_checked(self):
        self.write("docs/a.md", "Clean.\n")
        result = self.write("docs/a.md", "It's worth noting the cache.", tool="Edit")
        self.assertIn("CandourPhrases", result.context)
        self.assertIsNone(result.block_reason)

    def test_error_blocks_only_when_the_flag_is_on(self):
        self.write("docs/a.md", "Clean.\n")
        with mock.patch.object(gate, "blocking_enabled", lambda: True):
            result = self.write("docs/b.md", "To be fair, it broke.\n")
        self.assertIsNotNone(result.block_reason)
        self.assertIn("CandourPhrases", result.block_reason)
        self.assertIn(gate.STANDARD_DOC, result.block_reason)
        self.assertIsNone(result.context)
        with mock.patch.object(gate, "blocking_enabled", lambda: True):
            warning_only = self.write("docs/c.md", "In summary, fine.\n")
        self.assertIsNone(warning_only.block_reason)
        self.assertIn("SummaryCloser", warning_only.context)

    def test_findings_are_capped(self):
        self.write("docs/a.md", "Clean.\n")
        text = "\n\n".join("In summary, again." for _ in range(hook.MAX_FINDINGS_IN_CONTEXT + 5))
        result = self.write("docs/b.md", text)
        self.assertIn("... and 5 more", result.context)
        self.assertEqual(result.context.count("SummaryCloser"), hook.MAX_FINDINGS_IN_CONTEXT)

    def test_runner_subprocess_and_malformed_payloads_are_skipped(self):
        with mock.patch.dict(os.environ, {"APIARY_RUNNER_SUBPROCESS": "1"}):
            self.assertIsNone(self.write("docs/a.md", "To be fair, skipped.\n"))
        self.assertIsNone(hook.run({"tool_name": "Write", "tool_input": "not a dict"}))
        self.assertIsNone(hook.run({"tool_name": "Write", "tool_input": {}}))

    def test_no_session_id_still_checks_but_never_reminds(self):
        p = self.payload(
            "Write", file_path=str(self.repo / "docs/a.md"), content="In summary, x.\n"
        )
        p["session_id"] = ""
        result = hook.run(p)
        self.assertNotIn("Prose rules", result.context)
        self.assertIn("SummaryCloser", result.context)


if __name__ == "__main__":
    unittest.main()
