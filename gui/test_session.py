"""Unit tests for gui.session.Session input helpers.

Session.__init__ wires a pty, transcript discovery, scribe aggregator, and a
subagent tracker, none of which the input-surface logic needs. We build bare
instances with ``Session.__new__`` and attach a fake pty so these tests stay
fast and isolated from a real claude subprocess.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

from gui.session import Session, _claude_code_project_key, _replace_key_seps


class _FakePty:
    def __init__(self):
        self.sent = []

    def send_bytes(self, raw: bytes) -> bool:
        self.sent.append(raw)
        return True


def _session_with(pty):
    sess = Session.__new__(Session)
    sess.pty = pty
    return sess


class SendBytesTest(unittest.TestCase):
    def test_valid_int_list_forwards_exact_bytes(self):
        pty = _FakePty()
        sess = _session_with(pty)
        # Arrow-up: ESC [ A
        self.assertTrue(sess.send_bytes([27, 91, 65]))
        self.assertEqual(pty.sent, [b"\x1b[A"])

    def test_tuple_is_accepted(self):
        pty = _FakePty()
        sess = _session_with(pty)
        self.assertTrue(sess.send_bytes((13,)))  # carriage return
        self.assertEqual(pty.sent, [b"\r"])

    def test_empty_sequence_rejected(self):
        pty = _FakePty()
        sess = _session_with(pty)
        self.assertFalse(sess.send_bytes([]))
        self.assertEqual(pty.sent, [])

    def test_non_sequence_rejected(self):
        pty = _FakePty()
        sess = _session_with(pty)
        self.assertFalse(sess.send_bytes("AB"))
        self.assertFalse(sess.send_bytes(65))
        self.assertFalse(sess.send_bytes(None))
        self.assertEqual(pty.sent, [])

    def test_out_of_range_value_rejected(self):
        pty = _FakePty()
        sess = _session_with(pty)
        self.assertFalse(sess.send_bytes([256]))
        self.assertFalse(sess.send_bytes([-1]))
        self.assertFalse(sess.send_bytes([65, 999]))
        self.assertEqual(pty.sent, [])

    def test_no_pty_returns_false(self):
        sess = _session_with(None)
        self.assertFalse(sess.send_bytes([27, 91, 65]))


class SepReplacementTest(unittest.TestCase):
    """Core of the cwd → project-key transform, independent of platform.

    Kept separate from the full-path tests below because those can only run on
    the OS whose path flavour they spell — this covers the actual mapping rule
    everywhere.
    """

    def test_separators_dots_and_spaces_all_become_dashes(self):
        self.assertEqual(
            _replace_key_seps(r"Professional\Hexworld Rebuilt"),
            "Professional-Hexworld-Rebuilt",
        )
        self.assertEqual(_replace_key_seps("a/b.c d"), "a-b-c-d")

    def test_underscore_is_preserved(self):
        # Claude Code maps separators/dots/spaces to '-' but leaves '_' alone
        # (cf. the real dir D--Professional-job_search).
        self.assertEqual(_replace_key_seps("Professional/job_search"), "Professional-job_search")

    def test_already_clean_string_is_unchanged(self):
        self.assertEqual(_replace_key_seps("claude-apiary"), "claude-apiary")


@unittest.skipUnless(os.name == "nt", "Windows drive-letter path flavour")
class ProjectKeyWindowsTest(unittest.TestCase):
    """The cwd → ~/.claude/projects/<key> transform must match Claude Code's.

    A mismatch is silent and nasty: SessionDiscovery gets scoped to a dir that
    cannot exist, so the tab renders no assistant messages, no model name, and
    zero tokens, while the pty pane looks perfectly healthy.
    """

    def _key(self, path: str) -> str:
        # resolve() would rewrite these fixtures against the real filesystem;
        # the transform is pure string work, so stub it out.
        with mock.patch.object(Path, "resolve", lambda self: self):
            return _claude_code_project_key(Path(path))

    def test_space_is_replaced_with_dash(self):
        # Regression: D:\Professional\Hexworld Rebuilt rendered an empty chat
        # pane because the space survived into the lookup key.
        self.assertEqual(
            self._key(r"D:\Professional\Hexworld Rebuilt"),
            "D--Professional-Hexworld-Rebuilt",
        )

    def test_space_and_dot_together(self):
        self.assertEqual(
            self._key(r"D:\Professional\HexWorld 5.7"),
            "D--Professional-HexWorld-5-7",
        )

    def test_plain_path_unchanged_by_the_fix(self):
        self.assertEqual(
            self._key(r"D:\Professional\claude-apiary"),
            "D--Professional-claude-apiary",
        )


@unittest.skipIf(os.name == "nt", "POSIX rooted-path flavour")
class ProjectKeyPosixTest(unittest.TestCase):
    def _key(self, path: str) -> str:
        with mock.patch.object(Path, "resolve", lambda self: self):
            return _claude_code_project_key(Path(path))

    def test_rooted_path_with_space(self):
        self.assertEqual(self._key("/home/user/My Repo"), "-home-user-My-Repo")

    def test_plain_rooted_path(self):
        self.assertEqual(self._key("/home/user/claude-apiary"), "-home-user-claude-apiary")


if __name__ == "__main__":
    unittest.main()
