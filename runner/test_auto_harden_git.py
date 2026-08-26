"""commit_all must not sweep the operator's untracked files (review runner Bug 9)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from runner import auto_harden


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", check=False)


class CommitAllTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(["init", "-q", "-b", "main", "."], self.repo)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
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


if __name__ == "__main__":
    unittest.main()
