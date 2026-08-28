"""commit_all must not sweep the operator's untracked files (review runner Bug 9)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from runner import auto_harden


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=False
    )


class CommitAllTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        _git(["init", "-q", "-b", "main", "."], self.repo)
        for k, v in (
            ("user.email", "t@example.com"),
            ("user.name", "T"),
            ("commit.gpgsign", "false"),
        ):
            _git(["config", k, v], self.repo)
        (self.repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        _git(["add", "tracked.py"], self.repo)
        _git(["commit", "-q", "-m", "init"], self.repo)
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _committed(self):
        return _git(["show", "--name-only", "--format=", "HEAD"], self.repo).stdout.split()

    def test_untracked_scratch_is_left_alone(self):
        (self.repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "_tmp_scratch.txt").write_text("operator notes\n", encoding="utf-8")
        auto_harden.commit_all("harden round 1 fixes")
        self.assertEqual(self._committed(), ["tracked.py"])
        self.assertIn("_tmp_scratch.txt", _git(["status", "--porcelain"], self.repo).stdout)

    def test_declared_new_file_is_included(self):
        (self.repo / "new_module.py").write_text("y = 1\n", encoding="utf-8")
        (self.repo / "_tmp_scratch.txt").write_text("operator notes\n", encoding="utf-8")
        auto_harden.commit_all("harden round 1 fixes", ["new_module.py", "does_not_exist.py"])
        self.assertEqual(self._committed(), ["new_module.py"])

    def test_nothing_to_commit_is_fine(self):
        auto_harden.commit_all("noop")

    def test_files_created_since_snapshot_are_committed_even_if_undeclared(self):
        (self.repo / "_tmp_scratch.txt").write_text("operator notes\n", encoding="utf-8")
        before = auto_harden.untracked_files()
        self.assertIn("_tmp_scratch.txt", before)
        # The defender creates a helper the plan never declared and imports it.
        (self.repo / "foo_utils.py").write_text("def f(): pass\n", encoding="utf-8")
        (self.repo / "tracked.py").write_text("from foo_utils import f\n", encoding="utf-8")
        auto_harden.commit_all("harden round 1 fixes", ["tracked.py"], new_since=before)
        self.assertEqual(sorted(self._committed()), ["foo_utils.py", "tracked.py"])
        self.assertIn("_tmp_scratch.txt", _git(["status", "--porcelain"], self.repo).stdout)

    def test_bad_path_does_not_cancel_the_others_and_dirs_are_not_expanded(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "_tmp_scratch.py").write_text("x\n", encoding="utf-8")
        (self.repo / "new_mod.py").write_text("y = 1\n", encoding="utf-8")
        outside = Path(self._tmp.name).resolve().parent / "outside_apiary_probe.txt"
        outside.write_text("z\n", encoding="utf-8")
        try:
            auto_harden.commit_all("round", ["new_mod.py", str(outside), "tests/", "tests"])
        finally:
            outside.unlink(missing_ok=True)
        self.assertEqual(self._committed(), ["new_mod.py"])


if __name__ == "__main__":
    unittest.main()
