"""Tests for gui.pty_capture — path naming, CaptureWriter I/O, listing."""

from __future__ import annotations

import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path

from gui import pty_capture


class SanitizeLabelTests(unittest.TestCase):
    def test_safe_chars_preserved(self) -> None:
        self.assertEqual(pty_capture._sanitize_label("tool_permission"), "tool_permission")
        self.assertEqual(pty_capture._sanitize_label("plan-mode.v2"), "plan-mode.v2")

    def test_unsafe_chars_collapsed_and_trimmed(self) -> None:
        self.assertEqual(pty_capture._sanitize_label("hi there!"), "hi-there")
        self.assertEqual(pty_capture._sanitize_label("/weird///"), "weird")

    def test_empty_fallback(self) -> None:
        self.assertEqual(pty_capture._sanitize_label(""), "capture")
        self.assertEqual(pty_capture._sanitize_label("///"), "capture")


class NextCapturePathTests(unittest.TestCase):
    def test_creates_parent_dir_and_formats_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nested" / "captures"
            fixed = dt.datetime(2026, 4, 19, 14, 5, 33)
            p = pty_capture.next_capture_path("tool_permission", now=fixed, base=base)
            self.assertTrue(base.is_dir())
            self.assertEqual(p.name, "20260419-140533-tool_permission.bin")

    def test_no_label_suffix_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixed = dt.datetime(2026, 4, 19, 14, 5, 33)
            p = pty_capture.next_capture_path(None, now=fixed, base=Path(tmp))
            self.assertEqual(p.name, "20260419-140533.bin")

    def test_empty_label_sanitized_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixed = dt.datetime(2026, 4, 19, 14, 5, 33)
            p = pty_capture.next_capture_path("///", now=fixed, base=Path(tmp))
            self.assertEqual(p.name, "20260419-140533-capture.bin")


class CaptureWriterTests(unittest.TestCase):
    def test_writes_bytes_and_strings_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.bin"
            w = pty_capture.CaptureWriter(path)
            w.write(b"\x1b[1mhello\x1b[0m")
            w.write("  world\r\n")
            w.write(None)  # ignored
            w.write("")    # ignored
            w.write(42)    # ignored (non-bytes, non-str)
            w.close()
            data = path.read_bytes()
            self.assertEqual(data, b"\x1b[1mhello\x1b[0m  world\r\n")

    def test_close_is_idempotent_and_blocks_further_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.bin"
            w = pty_capture.CaptureWriter(path)
            w.write(b"abc")
            w.close()
            w.close()  # no-op
            w.write(b"after-close")  # silently dropped
            self.assertEqual(path.read_bytes(), b"abc")

    def test_context_manager_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.bin"
            with pty_capture.CaptureWriter(path) as w:
                w.write(b"x")
            # After __exit__ the handle is closed; writes are no-ops.
            w.write(b"y")
            self.assertEqual(path.read_bytes(), b"x")

    def test_concurrent_writes_do_not_lose_data_or_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.bin"
            w = pty_capture.CaptureWriter(path)
            payload = b"A" * 64

            def run() -> None:
                for _ in range(50):
                    w.write(payload)

            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            w.close()
            data = path.read_bytes()
            self.assertEqual(len(data), 4 * 50 * len(payload))
            # All bytes are 'A' — ordering doesn't matter, just no interleaving damage.
            self.assertTrue(all(b == ord("A") for b in data))


class ListCapturesTests(unittest.TestCase):
    def test_missing_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pty_capture.list_captures(Path(tmp) / "nope"), [])

    def test_returns_sorted_bin_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "20260419-100000-a.bin").write_bytes(b"")
            (base / "20260419-090000-b.bin").write_bytes(b"")
            (base / "ignored.txt").write_text("x", encoding="utf-8")
            result = pty_capture.list_captures(base)
            self.assertEqual([p.name for p in result],
                             ["20260419-090000-b.bin", "20260419-100000-a.bin"])


if __name__ == "__main__":
    unittest.main()
