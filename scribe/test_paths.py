#!/usr/bin/env python3
"""Tests for scribe/paths.py — the ``--project`` guard and the state-dir fallback."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe import paths


class ProjectKeyTests(unittest.TestCase):
    def test_a_plain_key_passes_through(self):
        self.assertEqual(
            paths.project_key("D--Professional-claude-apiary"), "D--Professional-claude-apiary"
        )

    def test_traversal_is_rejected(self):
        for bad in ("../escape", "a/b", r"a\b", "..", "a:b", "x" * 201, "a b"):
            with self.subTest(value=bad):
                with self.assertRaises(paths.ProjectKeyError):
                    paths.project_key(bad)

    def test_no_override_derives_from_cwd(self):
        with mock.patch.object(paths, "get_project_key", return_value="from-cwd") as g:
            self.assertEqual(paths.project_key(None), "from-cwd")
        g.assert_called_once()


class ResolveStoreDirTests(unittest.TestCase):
    def test_registry_answer_wins_over_the_legacy_fallback(self):
        with mock.patch.object(paths, "scribe_state_dir", return_value=Path("/state/scribe")):
            self.assertEqual(paths.resolve_store_dir(), Path("/state/scribe"))

    def test_falls_back_to_the_projects_dir(self):
        with mock.patch.object(paths, "scribe_state_dir", return_value=None):
            self.assertEqual(paths.resolve_store_dir("some-key"), paths.PROJECTS_DIR / "some-key")

    def test_a_bad_override_is_rejected_even_when_unused(self):
        # The fallback is not taken here — the override still has to be valid,
        # or a caller who got it wrong never finds out.
        with mock.patch.object(paths, "scribe_state_dir", return_value=Path("/state/scribe")):
            with self.assertRaises(paths.ProjectKeyError):
                paths.resolve_store_dir("../escape")


class SessionIdentityTests(unittest.TestCase):
    def test_returns_empty_strings_when_identity_is_unavailable(self):
        with mock.patch("core.session.load_identity", side_effect=OSError("boom")):
            self.assertEqual(paths.session_identity(), ("", "", ""))

    def test_reads_role_mission_and_session(self):
        identity = {"role": "attacker", "mission": "harden", "session_id": "abc12345"}
        with mock.patch("core.session.load_identity", return_value=identity):
            self.assertEqual(paths.session_identity(), ("attacker", "harden", "abc12345"))


if __name__ == "__main__":
    unittest.main()
