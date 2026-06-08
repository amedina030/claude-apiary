"""Tests for gui.pty_wrapper — focuses on send_text chunking behavior.

Does not spawn a real pty; the test installs a stub on ``_proc`` and asserts
that send_text round-trips arbitrary-length input by accumulating chunks.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

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


class WaitForQuietTests(unittest.TestCase):
    """wait_for_quiet() gates the submit CR after a bracketed paste so it isn't
    sent before the CLI finishes rendering the paste (the double-Enter bug)."""

    def test_returns_true_after_activity_settles(self) -> None:
        wrapper = PtyWrapper()
        # Output already arrived after the baseline, then went quiet.
        wrapper._last_out_at = time.monotonic()
        start = time.monotonic()
        self.assertTrue(wrapper.wait_for_quiet(after=0.0, quiet=0.05, timeout=1.0))
        # It must actually wait out the quiet window, not return instantly.
        self.assertGreaterEqual(time.monotonic() - start, 0.05)

    def test_idle_prompt_does_not_trigger_early_cr(self) -> None:
        # No output ever arrives after the baseline (stale quiet from an idle
        # prompt must NOT count as "settled"). Should wait the full timeout and
        # report failure rather than firing the CR immediately.
        wrapper = PtyWrapper()
        baseline = wrapper._last_out_at  # 0.0, no output seen
        start = time.monotonic()
        self.assertFalse(wrapper.wait_for_quiet(after=baseline, quiet=0.05, timeout=0.1))
        self.assertGreaterEqual(time.monotonic() - start, 0.1)

    def test_continuous_output_times_out(self) -> None:
        # A spinner that never goes quiet hits the timeout floor and returns
        # False — the caller still sends the CR, just later.
        wrapper = PtyWrapper()
        stop = threading.Event()

        def churn() -> None:
            while not stop.is_set():
                wrapper._last_out_at = time.monotonic()
                time.sleep(0.01)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            self.assertFalse(wrapper.wait_for_quiet(after=0.0, quiet=0.08, timeout=0.2))
        finally:
            stop.set()
            t.join(timeout=1.0)

    def test_stop_event_short_circuits(self) -> None:
        wrapper = PtyWrapper()
        wrapper._stop.set()
        self.assertFalse(wrapper.wait_for_quiet(after=0.0, quiet=0.05, timeout=5.0))


class ResolveClaudeCommandTests(unittest.TestCase):
    """T-2026-232: a .cmd npm shim must be wrapped through cmd.exe, and a real
    .exe preferred, so ConPTY does not fail with WinError 193."""

    def _patch(self, *, os_name: str, which_map: dict):
        which_patch = mock.patch.object(
            pty_wrapper.shutil, "which", side_effect=lambda n: which_map.get(n)
        )
        os_patch = mock.patch.object(pty_wrapper.os, "name", os_name)
        return which_patch, os_patch

    def test_missing_returns_none(self) -> None:
        wp, op = self._patch(os_name="nt", which_map={})
        with wp, op:
            self.assertIsNone(pty_wrapper._resolve_claude_command("claude"))

    def test_windows_cmd_shim_wrapped_through_cmd_exe(self) -> None:
        wp, op = self._patch(
            os_name="nt",
            which_map={"claude.exe": None, "claude": r"C:\npm\claude.cmd"},
        )
        with wp, op:
            self.assertEqual(
                pty_wrapper._resolve_claude_command("claude"),
                ["cmd", "/c", r"C:\npm\claude.cmd"],
            )

    def test_windows_prefers_real_exe_over_shim(self) -> None:
        wp, op = self._patch(
            os_name="nt",
            which_map={"claude.exe": r"C:\Program Files\claude\claude.exe",
                       "claude": r"C:\npm\claude.cmd"},
        )
        with wp, op:
            self.assertEqual(
                pty_wrapper._resolve_claude_command("claude"),
                [r"C:\Program Files\claude\claude.exe"],
            )

    def test_posix_returns_path_as_is(self) -> None:
        wp, op = self._patch(
            os_name="posix", which_map={"claude": "/usr/local/bin/claude"}
        )
        with wp, op:
            self.assertEqual(
                pty_wrapper._resolve_claude_command("claude"),
                ["/usr/local/bin/claude"],
            )


if __name__ == "__main__":
    unittest.main()
