#!/usr/bin/env python3
"""Unit tests for pre_push_doc_conformer.command_pushes — the pure detector.

The subprocess shell (_run) shells out to git and the conformer; the bit
that decides *whether a Bash command is a push* is pure and is where the
false-positive / false-negative risk lives, so that's what's tested here.
"""

import unittest

from core.hooks.pre_push_doc_conformer import command_pushes


class CommandPushesTest(unittest.TestCase):
    def test_plain_push(self):
        self.assertTrue(command_pushes("git push"))

    def test_push_with_args(self):
        self.assertTrue(command_pushes("git push -u origin feat/x"))
        self.assertTrue(command_pushes("git push --force-with-lease"))

    def test_push_with_global_value_flag(self):
        # The -C <path> global option consumes its value; push still detected.
        self.assertTrue(command_pushes("git -C /repo push origin master"))
        self.assertTrue(command_pushes("git -c protocol.version=2 push"))

    def test_push_in_a_chained_segment(self):
        self.assertTrue(command_pushes("git commit -m wip && git push"))
        self.assertTrue(command_pushes("git add -A; git commit -m x; git push origin master"))

    def test_env_prefixed_push(self):
        self.assertTrue(command_pushes("GIT_SSH_COMMAND=ssh git push"))

    def test_not_a_push_other_git_subcommands(self):
        for cmd in (
            "git status",
            "git commit -m 'add push button'",
            "git log --oneline",
            "git pull origin master",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(command_pushes(cmd))

    def test_word_push_without_git_subcommand(self):
        # The literal word appears but no `git push` runs → must not fire,
        # otherwise a dirty-docs tree would wrongly block harmless commands.
        for cmd in (
            'echo "git push"',
            "grep push file.txt",
            "git pushup",
            "cat docs/push-guide.md",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(command_pushes(cmd))

    def test_empty_and_none(self):
        self.assertFalse(command_pushes(""))
        self.assertFalse(command_pushes(None))


if __name__ == "__main__":
    unittest.main()
