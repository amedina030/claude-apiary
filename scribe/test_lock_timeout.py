"""A FileLock timeout inside any scribe command is one line + exit 1, not a traceback."""

import io
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from scribe import notes


class LockTimeoutTest(unittest.TestCase):
    def test_timeout_is_reported_not_raised(self):
        def busy(_args):
            raise TimeoutError("Could not acquire lock: x.lock")

        err = io.StringIO()
        with (
            mock.patch.object(notes, "COMMANDS", {"list": busy}),
            mock.patch.object(notes, "_apply_session_identity", lambda a: None),
            mock.patch.object(notes.paths, "resolve_store_dir", lambda p: "unused"),
            mock.patch.object(notes, "ScribeStore", lambda d: object()),
            mock.patch.object(sys, "argv", ["notes.py", "list"]),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as cm:
                notes.main()
        self.assertEqual(cm.exception.code, 1)
        text = err.getvalue()
        self.assertIn("index busy", text)
        self.assertIn("repair", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
