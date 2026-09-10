"""Tests for prose/gate.py: the (message, fatal) contract under each blocking mode."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import engine, gate


class GateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        style = Path(self._tmp.name) / "T"
        style.mkdir()
        (style / "Err.toml").write_text(
            'extends = "existence"\nmessage = "error phrase %s"\ntokens = ["to be clear"]\nignorecase = true\nlevel = "error"\n',
            encoding="utf-8",
        )
        (style / "Warn.toml").write_text(
            'extends = "existence"\nmessage = "warn phrase %s"\ntokens = ["in summary"]\nignorecase = true\nlevel = "warning"\n',
            encoding="utf-8",
        )
        (style / "Sug.toml").write_text(
            'extends = "existence"\nmessage = "sug %s"\ntokens = ["utilize"]\nignorecase = true\nlevel = "suggestion"\n',
            encoding="utf-8",
        )
        self.cfg = dict(engine.DEFAULT_CONFIG)
        self.cfg.update({"styles": [], "style_paths": [str(style)]})

    def gate(self, text, **kw):
        return gate.gate(text, config=self.cfg, **kw)

    def test_clean_text(self):
        self.assertEqual(self.gate("Nothing to see.", blocking=True), (None, False))

    def test_suggestions_alone_are_below_the_default_floor(self):
        self.assertEqual(self.gate("We utilize it.", blocking=True), (None, False))
        msg, fatal = self.gate("We utilize it.", blocking=True, min_level="suggestion")
        self.assertFalse(fatal)
        self.assertIn("Sug", msg)

    def test_advisory_when_blocking_off(self):
        msg, fatal = self.gate("To be clear, it works. In summary, done.", blocking=False)
        self.assertFalse(fatal)
        self.assertIn("1 error, 1 warning", msg)
        self.assertIn("Advisory", msg)
        self.assertIn(gate.STANDARD_DOC, msg)

    def test_blocking_with_error_is_fatal_and_names_force(self):
        msg, fatal = self.gate("To be clear, it works.", blocking=True)
        self.assertTrue(fatal)
        self.assertIn("--force", msg)
        self.assertIn(gate.FORCED_TAG, msg)
        self.assertIn(gate.READ_REMINDER, msg)

    def test_blocking_with_only_warnings_is_not_fatal(self):
        msg, fatal = self.gate("In summary, done.", blocking=True)
        self.assertFalse(fatal)
        self.assertIn("No error-level finding", msg)

    def test_force_bypasses_and_says_so(self):
        msg, fatal = self.gate("To be clear, it works.", blocking=True, force=True)
        self.assertFalse(fatal)
        self.assertTrue(msg.startswith("[prose gate bypassed via --force: 1 error(s)]"))

    def test_blocking_default_reads_the_flag(self):
        # Outside a bootstrapped repo the lookup resolves to "off" rather than raising.
        self.assertIn(gate.blocking_enabled(), (True, False))


if __name__ == "__main__":
    unittest.main()
