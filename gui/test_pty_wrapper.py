"""Tests for gui.pty_wrapper — focuses on send_text chunking behavior.

Does not spawn a real pty; the test installs a stub on ``_proc`` and asserts
that send_text round-trips arbitrary-length input by accumulating chunks.
"""

from __future__ import annotations

import unittest

from gui import pty_wrapper
from gui.pty_wrapper import PtyWrapper


class _StubProc:
    def __init__(self, *, alive: bool = True, raise_on_write_after: int | None = None):
        self.writes: list[str] = []
        self._alive = alive
        self._raise_after = raise_on_write_after

    def write(self, s: str) -> None:
        if self._raise_after is not None and len(self.writes) >= self._raise_after:
            raise RuntimeError("simulated pty write failure")
        self.writes.append(s)

    def isalive(self) -> bool:
        return self._alive


class SendTextChunkingTests(unittest.TestCase):
    def _wrapper_with_stub(self, **stub_kwargs) -> tuple[PtyWrapper, _StubProc]:
        wrapper = PtyWrapper()
        stub = _StubProc(**stub_kwargs)
        wrapper._proc = stub  # type: ignore[assignment]
        return wrapper, stub

    def test_short_text_writes_once(self) -> None:
        wrapper, stub = self._wrapper_with_stub()
        self.assertTrue(wrapper.send_text("hello"))
        self.assertEqual(stub.writes, ["hello"])

    def test_empty_string_is_a_noop_but_succeeds(self) -> None:
        wrapper, stub = self._wrapper_with_stub()
        self.assertTrue(wrapper.send_text(""))
        self.assertEqual(stub.writes, [])

    def test_long_paste_round_trips_intact(self) -> None:
        wrapper, stub = self._wrapper_with_stub()
        # 5 KB — the size class that exposed the original truncation bug.
        long_text = ("abcdefghij" * 600)[:5500]
        self.assertTrue(wrapper.send_text(long_text))
        self.assertGreater(len(stub.writes), 1)
        self.assertEqual("".join(stub.writes), long_text)

    def test_each_chunk_under_threshold(self) -> None:
        wrapper, stub = self._wrapper_with_stub()
        wrapper.send_text("X" * 5000)
        for chunk in stub.writes:
            self.assertLessEqual(len(chunk), pty_wrapper._SEND_CHUNK_SIZE)

    def test_dead_pty_returns_false(self) -> None:
        wrapper, _stub = self._wrapper_with_stub(alive=False)
        self.assertFalse(wrapper.send_text("hello"))

    def test_no_pty_returns_false(self) -> None:
        wrapper = PtyWrapper()
        # _proc is None (start() never called)
        self.assertFalse(wrapper.send_text("hello"))

    def test_write_exception_mid_stream_returns_false(self) -> None:
        wrapper, stub = self._wrapper_with_stub(raise_on_write_after=2)
        # Long enough to need >2 chunks.
        self.assertFalse(wrapper.send_text("Y" * 4000))
        # Confirms partial writes before the failure; we don't claim atomicity.
        self.assertEqual(len(stub.writes), 2)


if __name__ == "__main__":
    unittest.main()
