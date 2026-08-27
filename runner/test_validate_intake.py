#!/usr/bin/env python3
"""Tests for runner/validate_intake.py — field validation logic."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from runner.validate_intake import validate


def _git_init(path: Path) -> None:
    """Init a bare-minimum git repo so target_repo validation accepts it."""
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
    )


def _base_intake() -> dict:
    """A minimal fully-valid intake dict that can be mutated per-test."""
    return {
        "id": "a1b2c3d4",
        "title": "X",
        "problem": "A problem that is at least twenty characters long",
        "description": "A description that is at least twenty characters long",
        "scope": "runner/",
        "created_at": "2026-04-16T12:00:00Z",
    }


class TestBaseValidation(unittest.TestCase):
    """Smoke: the base fixture passes; known missing fields fail."""

    def test_base_is_valid(self):
        self.assertEqual(validate(_base_intake()), [])

    def test_missing_required_field(self):
        intake = _base_intake()
        del intake["problem"]
        errs = validate(intake)
        self.assertTrue(any("problem" in e for e in errs))


class TestTargetRepoField(unittest.TestCase):
    """Phase 3: optional target_repo field validation."""

    def test_absent_target_repo_is_fine(self):
        """Backwards-compat: an intake without target_repo is valid."""
        self.assertEqual(validate(_base_intake()), [])

    def test_valid_target_repo_passes(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve() / "r"
            repo.mkdir()
            _git_init(repo)
            intake = _base_intake()
            intake["target_repo"] = str(repo)
            self.assertEqual(validate(intake), [])

    def test_target_repo_wrong_type_rejected(self):
        intake = _base_intake()
        intake["target_repo"] = 42
        errs = validate(intake)
        self.assertTrue(any("target_repo must be a string" in e for e in errs))

    def test_target_repo_empty_string_rejected(self):
        intake = _base_intake()
        intake["target_repo"] = "   "
        errs = validate(intake)
        self.assertTrue(any("target_repo is empty" in e for e in errs))

    def test_target_repo_nonexistent_path_rejected(self):
        intake = _base_intake()
        intake["target_repo"] = str(Path(tempfile.gettempdir()) / "does-not-exist-x9z2q")
        errs = validate(intake)
        self.assertTrue(any("does not exist" in e for e in errs))

    def test_target_repo_not_a_directory_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td).resolve() / "file.txt"
            f.write_text("x", encoding="utf-8")
            intake = _base_intake()
            intake["target_repo"] = str(f)
            errs = validate(intake)
            self.assertTrue(any("not a directory" in e for e in errs))

    def test_target_repo_missing_dot_git_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            intake = _base_intake()
            intake["target_repo"] = td  # a plain dir, no .git
            errs = validate(intake)
            self.assertTrue(any("not a git repository" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
