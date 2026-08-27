"""Tests for theme loading + watcher debouncing + single-instance."""

from __future__ import annotations

import json
import tempfile
import threading
import os
import unittest
from pathlib import Path

from gui.single_instance import SingleInstance
from gui.theme import DEFAULT_THEME, ThemeWatcher, load_theme


class LoadThemeTests(unittest.TestCase):
    def test_missing_file_returns_defaults_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "theme.json"
            vars_, err = load_theme(p)
            self.assertIsNone(err)
            self.assertEqual(vars_, dict(DEFAULT_THEME))

    def test_malformed_json_returns_defaults_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "theme.json"
            p.write_text("{not json", encoding="utf-8")
            vars_, err = load_theme(p)
            self.assertIsNotNone(err)
            self.assertEqual(vars_, dict(DEFAULT_THEME))

    def test_non_object_returns_defaults_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "theme.json"
            p.write_text("[]", encoding="utf-8")
            _vars, err = load_theme(p)
            self.assertIsNotNone(err)
            assert err is not None
            self.assertIn("object", err)

    def test_partial_overrides_merge_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "theme.json"
            p.write_text(json.dumps({"--user-msg-bg": "#ff0000"}), encoding="utf-8")
            vars_, err = load_theme(p)
            self.assertIsNone(err)
            self.assertEqual(vars_["--user-msg-bg"], "#ff0000")
            # Other defaults still present.
            self.assertEqual(vars_["--bg"], DEFAULT_THEME["--bg"])


class ThemeWatcherTests(unittest.TestCase):
    def test_watcher_fires_after_save_with_debounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "theme.json"
            p.write_text(json.dumps({"--accent": "#000000"}), encoding="utf-8")

            received: list[tuple[dict, str | None]] = []
            event = threading.Event()

            def on_reload(vars_, err):
                received.append((vars_, err))
                event.set()

            watcher = ThemeWatcher(on_reload=on_reload, path=p, debounce=0.1)
            watcher.start()
            try:
                # Initial fire from start().
                self.assertTrue(event.wait(timeout=2.0))
                self.assertEqual(received[0][0]["--accent"], "#000000")
                event.clear()

                # Modify and confirm reload.
                p.write_text(json.dumps({"--accent": "#ff0000"}), encoding="utf-8")
                self.assertTrue(event.wait(timeout=3.0))
                self.assertEqual(received[-1][0]["--accent"], "#ff0000")
            finally:
                watcher.stop()


@unittest.skipUnless(os.name == "nt", "SingleInstance is a Win32 named mutex; on POSIX nothing holds it and the second instance also acquires (T-2026-292)")
class SingleInstanceTests(unittest.TestCase):
    def test_first_acquires_second_does_not(self):
        name = "Local\\apiary_gui_test_singleton_xyz"
        with SingleInstance(name) as a:
            self.assertTrue(a.acquired)
            with SingleInstance(name) as b:
                self.assertFalse(b.acquired)


if __name__ == "__main__":
    unittest.main()
