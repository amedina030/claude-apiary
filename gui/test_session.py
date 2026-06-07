"""Unit tests for gui.session.Session input helpers.

Session.__init__ wires a pty, transcript discovery, scribe aggregator, and a
subagent tracker, none of which the input-surface logic needs. We build bare
instances with ``Session.__new__`` and attach a fake pty so these tests stay
fast and isolated from a real claude subprocess.
"""

import unittest

from gui.session import Session


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


if __name__ == "__main__":
    unittest.main()
