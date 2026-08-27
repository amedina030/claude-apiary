"""Tests for core/utils/gitutil.py — the one git-root resolver."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils.gitutil import git_root, main_worktree_root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


class GitRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # macOS /var -> /private/var: resolve so comparisons match git's answer.
        self.root = Path(self._tmp.name).resolve()

    def _repo(self, name: str = "repo") -> Path:
        repo = self.root / name
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        _git("commit", "--allow-empty", "-q", "-m", "init", cwd=repo)
        return repo

    def test_returns_repo_root_from_the_repo_itself(self) -> None:
        repo = self._repo()
        self.assertEqual(git_root(repo), repo)

    def test_returns_repo_root_from_a_subdirectory(self) -> None:
        repo = self._repo()
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(git_root(nested), repo)

    def test_accepts_a_string_path(self) -> None:
        repo = self._repo()
        self.assertEqual(git_root(str(repo)), repo)

    def test_outside_a_repo_returns_none(self) -> None:
        # A bare tempdir is not inside a work tree — unless the OS temp dir
        # itself happens to live in one, which no CI or dev box does.
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(git_root(plain))

    def test_missing_start_directory_returns_none(self) -> None:
        self.assertIsNone(git_root(self.root / "does-not-exist"))

    def test_missing_git_binary_returns_none(self) -> None:
        repo = self._repo()
        with mock.patch("core.utils.gitutil.subprocess.run",
                        side_effect=FileNotFoundError("no git")):
            self.assertIsNone(git_root(repo))

    def test_git_timeout_returns_none_rather_than_raising(self) -> None:
        repo = self._repo()
        with mock.patch(
            "core.utils.gitutil.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            self.assertIsNone(git_root(repo))


class MainWorktreeRootTests(unittest.TestCase):
    """The precedence fix behind ``resolve_apiary_repo``: a linked worktree
    must resolve to the checkout it was cut from, not to itself."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.main = self.root / "main"
        self.main.mkdir()
        _git("init", "-q", cwd=self.main)
        (self.main / "f.txt").write_text("x", encoding="utf-8")
        _git("add", "f.txt", cwd=self.main)
        _git("commit", "-q", "-m", "init", cwd=self.main)

    def test_main_checkout_resolves_to_itself(self) -> None:
        self.assertEqual(main_worktree_root(self.main), self.main)

    def test_linked_worktree_resolves_to_the_main_checkout(self) -> None:
        wt = self.root / "wt"
        _git("worktree", "add", "--detach", "-q", str(wt), cwd=self.main)
        # git_root sees the worktree; main_worktree_root sees through it.
        self.assertEqual(git_root(wt), wt)
        self.assertEqual(main_worktree_root(wt), self.main)

    def test_outside_a_repo_returns_none(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(main_worktree_root(plain))

    def test_falls_back_to_git_root_on_old_git_without_path_format(self) -> None:
        # git < 2.31 rejects --path-format; the helper must degrade to the
        # plain work-tree root rather than reporting "not a repo".
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if "--path-format=absolute" in cmd:
                return subprocess.CompletedProcess(cmd, 129, "", "unknown option")
            return real_run(cmd, *a, **kw)

        with mock.patch("core.utils.gitutil.subprocess.run", side_effect=fake_run):
            self.assertEqual(main_worktree_root(self.main), self.main)


if __name__ == "__main__":
    unittest.main()
