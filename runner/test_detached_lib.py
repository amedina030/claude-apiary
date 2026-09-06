#!/usr/bin/env python3
"""Unit tests for runner/detached_lib.py."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import detached_lib


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(detached_lib.slugify("Hello World!"), "hello-world")

    def test_empty(self):
        self.assertEqual(detached_lib.slugify(""), "item")

    def test_unicode(self):
        self.assertTrue(detached_lib.slugify("A B C").startswith("a-b"))


_REPO = Path("/tmp/some-target-repo")


class TestHygiene(unittest.TestCase):
    def test_ok_when_below_max(self):
        with mock.patch.object(
            detached_lib, "list_unmerged_runner_branches", return_value=["runner/a-1", "runner/b-2"]
        ):
            self.assertIsNone(detached_lib.hygiene_precheck(5, _REPO))

    def test_full(self):
        with mock.patch.object(
            detached_lib,
            "list_unmerged_runner_branches",
            return_value=["runner/a", "runner/b", "runner/c", "runner/d", "runner/e"],
        ):
            reason = detached_lib.hygiene_precheck(5, _REPO)
            self.assertIn("queue full", reason)
            self.assertIn("5/5", reason)

    def test_passes_repo_through(self):
        """The repo argument reaches the branch listing — a hygiene check
        against apiary while running against another target is Bug 2."""
        seen = []
        with mock.patch.object(
            detached_lib,
            "list_unmerged_runner_branches",
            side_effect=lambda repo, base="master": seen.append(repo) or [],
        ):
            detached_lib.hygiene_precheck(5, _REPO)
        self.assertEqual(seen, [_REPO])


class TestPickBacklog(unittest.TestCase):
    def test_picks_oldest_not_claimed(self):
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td).resolve() / "backlog"
            bdir.mkdir()
            # create two items, write older one first
            a = bdir / "a.json"
            b = bdir / "b.json"
            a.write_text(json.dumps({"id": "uuid-a", "title": "A"}), encoding="utf-8")
            b.write_text(json.dumps({"id": "uuid-b", "title": "B"}), encoding="utf-8")
            import os

            os.utime(a, (1000, 1000))
            os.utime(b, (2000, 2000))
            with mock.patch.object(detached_lib, "BACKLOG_DIR", bdir):
                with mock.patch.object(detached_lib, "list_runner_branches", return_value=[]):
                    p = detached_lib.pick_backlog_item(_REPO)
                    self.assertEqual(p.name, "a.json")
                with mock.patch.object(
                    detached_lib, "list_runner_branches", return_value=["runner/a-xxxxxxxx-uuid-a"]
                ):
                    p = detached_lib.pick_backlog_item(_REPO)
                    self.assertEqual(p.name, "b.json")

    def test_item_target_repo_overrides_default(self):
        """An item that names its own target_repo is checked for an
        in-flight branch in THAT repo, not the invocation's default."""
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td).resolve() / "backlog"
            bdir.mkdir()
            item = bdir / "a.json"
            item.write_text(
                json.dumps({"id": "uuid-a", "title": "A", "target_repo": "/tmp/other-repo"}),
                encoding="utf-8",
            )
            seen = []
            with (
                mock.patch.object(detached_lib, "BACKLOG_DIR", bdir),
                mock.patch.object(
                    detached_lib,
                    "list_runner_branches",
                    side_effect=lambda repo: seen.append(repo) or [],
                ),
            ):
                detached_lib.pick_backlog_item(_REPO)
            self.assertEqual(seen, [Path("/tmp/other-repo")])

    def test_empty(self):
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td).resolve() / "backlog"
            bdir.mkdir()
            with mock.patch.object(detached_lib, "BACKLOG_DIR", bdir):
                self.assertIsNone(detached_lib.pick_backlog_item(_REPO))


class TestGitRequiresCwd(unittest.TestCase):
    """_git's cwd is mandatory: an omitted cwd used to mean 'apiary'."""

    def test_git_without_cwd_is_a_type_error(self):
        with self.assertRaises(TypeError):
            detached_lib._git(["status"])


class TestGitCommitAllIn(unittest.TestCase):
    """The bundle commit sweeps the worktree with `git add -A`, so
    gitignored paths stay out of the runner branch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        import subprocess

        for args in (
            ["init", "--initial-branch=master"],
            ["config", "user.email", "t@e.com"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=str(self.root), check=True, capture_output=True)
        (self.root / ".gitignore").write_text(
            "runner/specs/\nrunner/plans/\nrunner/executions/\nrunner/hardens/\nrunner/reports/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", ".gitignore"], cwd=str(self.root), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(self.root), check=True, capture_output=True
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _make_artifact(self, subdir: str, uuid: str):
        d = self.root / "runner" / subdir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{uuid}.json"
        p.write_text(json.dumps({"uuid": uuid, "from": subdir}), encoding="utf-8")
        return p

    def test_commit_sweeps_worktree_but_not_gitignored_paths(self):
        import subprocess

        uuid = "gitignored-uuid"
        self._make_artifact("specs", uuid)
        (self.root / "README.md").write_text("hello\n", encoding="utf-8")
        ok, err = detached_lib.git_commit_all_in(
            self.root,
            "runner/gitignored-uuid: test",
        )
        self.assertTrue(ok, err)
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "HEAD", "--name-only"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("README.md", tree.stdout)
        self.assertNotIn(f"runner/specs/{uuid}.json", tree.stdout)


class TestWorktreesDirFor(unittest.TestCase):
    """Phase 1 multi-repo: target-repo aware worktree directory."""

    def test_default_returns_apiary_worktrees_dir(self):
        self.assertEqual(
            detached_lib.worktrees_dir_for(None),
            detached_lib.WORKTREES_DIR,
        )

    def test_custom_target_returns_target_subdir(self):
        custom = Path("/tmp/some/scratch_repo").resolve()
        self.assertEqual(
            detached_lib.worktrees_dir_for(custom),
            custom / ".runner-worktrees",
        )

    def test_agrees_with_target_repo_helper(self):
        """One definition: prune scans exactly what create writes to."""
        from runner import target_repo as target_repo_mod

        custom = Path("/tmp/some/scratch_repo").resolve()
        self.assertEqual(
            detached_lib.worktrees_dir_for(custom),
            target_repo_mod.worktrees_dir(custom),
        )


class TestGitWorktreeCreateTargetRepo(unittest.TestCase):
    """Phase 1 multi-repo: git_worktree_create operates on the given target repo.

    Real git operations against a tempdir prove end-to-end that the worktree
    lands under the target's .runner-worktrees/ and the new branch is created
    on the target's git tree, not apiary's.
    """

    def setUp(self):
        import subprocess

        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name).resolve() / "target_repo"
        self.target.mkdir()
        for args in (
            ["init", "--initial-branch=master"],
            ["config", "user.email", "t@e.com"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=str(self.target), check=True, capture_output=True)
        (self.target / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.target), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(self.target), check=True, capture_output=True
        )

    def tearDown(self):
        # Best-effort: tear down any worktree so the tempdir cleanup
        # doesn't trip on git's metadata locks.
        import subprocess

        wt_root = self.target / ".runner-worktrees"
        if wt_root.exists():
            for entry in wt_root.iterdir():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(entry)],
                    cwd=str(self.target),
                    capture_output=True,
                )
        self._tmp.cleanup()

    def test_creates_worktree_under_target_repo(self):
        ok, wt_path, err = detached_lib.git_worktree_create(
            "runner/test-branch",
            target_repo=self.target,
        )
        self.assertTrue(ok, err)
        self.assertIsNotNone(wt_path)
        # Worktree must live under target/.runner-worktrees/, NOT apiary's
        expected_root = self.target / ".runner-worktrees"
        self.assertTrue(
            str(wt_path).startswith(str(expected_root)),
            f"worktree {wt_path} not under {expected_root}",
        )
        self.assertTrue(wt_path.exists())
        self.assertTrue(
            (wt_path / "README.md").exists(),
            "worktree should contain target repo files, not apiary",
        )

    def test_branch_created_on_target_not_apiary(self):
        import subprocess

        ok, _wt, err = detached_lib.git_worktree_create(
            "runner/branch-on-target",
            target_repo=self.target,
        )
        self.assertTrue(ok, err)
        result = subprocess.run(
            ["git", "branch", "--list", "runner/branch-on-target"],
            cwd=str(self.target),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("runner/branch-on-target", result.stdout)


if __name__ == "__main__":
    unittest.main()


class TestGitWorktreeRemoveFallback(unittest.TestCase):
    """T-2026-303: git's 'Filename too long' on a venv-bearing worktree no
    longer ends the night as worktree_remove_failed; the tree is deleted
    through the long-path helper and the registration pruned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.wt = Path(self._tmp.name) / "wt"
        (self.wt / "deep" / "dir").mkdir(parents=True)
        (self.wt / "deep" / "dir" / "f.txt").write_text("x", encoding="utf-8")

    @staticmethod
    def _proc(rc, stderr=""):
        import subprocess

        return subprocess.CompletedProcess(["git"], rc, stdout="", stderr=stderr)

    def test_missing_path_is_ok_without_calling_git(self):
        with mock.patch.object(detached_lib, "_git") as git:
            ok, err = detached_lib.git_worktree_remove(self.wt / "absent", target_repo=self.repo)
        self.assertEqual((ok, err), (True, ""))
        git.assert_not_called()

    def test_git_success_is_ok(self):
        with mock.patch.object(detached_lib, "_git", return_value=self._proc(0)) as git:
            ok, err = detached_lib.git_worktree_remove(self.wt, target_repo=self.repo)
        self.assertEqual((ok, err), (True, ""))
        self.assertEqual(git.call_count, 1)

    def test_git_failure_falls_back_to_long_path_rmtree_and_prunes(self):
        calls = []

        def fake_git(args, cwd=None):
            calls.append(list(args))
            if args[:2] == ["worktree", "remove"]:
                return self._proc(1, "error: failed to delete: Filename too long")
            return self._proc(0)

        with mock.patch.object(detached_lib, "_git", side_effect=fake_git):
            ok, err = detached_lib.git_worktree_remove(self.wt, target_repo=self.repo)
        self.assertEqual((ok, err), (True, ""))
        self.assertFalse(self.wt.exists())
        self.assertEqual(calls[-1], ["worktree", "prune"])

    def test_reports_both_errors_when_the_fallback_fails_too(self):
        with (
            mock.patch.object(
                detached_lib, "_git", return_value=self._proc(1, "Filename too long")
            ),
            mock.patch.object(detached_lib, "rmtree_long", side_effect=OSError("locked")),
        ):
            ok, err = detached_lib.git_worktree_remove(self.wt, target_repo=self.repo)
        self.assertFalse(ok)
        self.assertIn("Filename too long", err)
        self.assertIn("rmtree fallback failed: locked", err)
        self.assertTrue(self.wt.exists())


class TestGitWorktreeReuse(unittest.TestCase):
    """T-2026-302: a preserved worktree/branch is reused on the next attempt
    instead of failing `git worktree add -b` with 'branch already exists'."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(self.repo), check=True)
        (self.repo / "README").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(self.repo), check=True)
        self.branch = "runner/slug-abcd1234"

    def _git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _create(self):
        ok, wt, err = detached_lib.git_worktree_create(self.branch, target_repo=self.repo)
        self.assertTrue(ok, err)
        return wt

    def test_nothing_preserved_means_fresh_create(self):
        found, wt, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertEqual((found, wt, err), (False, None, ""))

    def test_registered_worktree_is_reset_and_reused(self):
        wt = self._create()
        (wt / "README").write_text("dirty\n", encoding="utf-8")  # the failed step's edit
        (wt / "stray.txt").write_text("x", encoding="utf-8")  # untracked leftover
        (wt / ".venv").mkdir()  # what an agent leaves behind; ignored or not, it goes
        found, path, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertEqual((found, err), (True, ""))
        self.assertEqual(path.resolve(), wt.resolve())
        self.assertEqual((wt / "README").read_text(encoding="utf-8"), "x\n")
        self.assertFalse((wt / "stray.txt").exists())
        self.assertFalse((wt / ".venv").exists())
        head = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip()
        self.assertEqual(head, self.branch)

    def test_branch_without_directory_gets_a_worktree_on_that_branch(self):
        wt = self._create()
        (wt / "step.txt").write_text("committed work\n", encoding="utf-8")
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-q", "-m", "runner/abcd1234 step 1: work", cwd=wt)
        self.assertEqual(self._git("worktree", "remove", "--force", str(wt)).returncode, 0)
        self.assertFalse(wt.exists())
        found, path, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertEqual((found, err), (True, ""))
        self.assertTrue((path / "step.txt").exists(), "the committed step must be back")
        head = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).stdout.strip()
        self.assertEqual(head, self.branch)

    def test_orphaned_directory_is_replaced(self):
        wt = self._create()
        self._git("worktree", "remove", "--force", str(wt))
        wt.mkdir(parents=True)  # orphan: on disk, not registered
        (wt / "junk").write_text("x", encoding="utf-8")
        found, path, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertEqual((found, err), (True, ""))
        self.assertFalse((path / "junk").exists())
        self.assertTrue((path / "README").exists())

    def test_orphaned_directory_without_branch_is_removed_then_fresh(self):
        wt_dir = detached_lib.worktrees_dir_for(self.repo) / self.branch.replace("/", "_")
        wt_dir.mkdir(parents=True)
        found, path, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertEqual((found, path, err), (False, None, ""))
        self.assertFalse(wt_dir.exists())

    def test_path_on_another_branch_is_an_error(self):
        wt = self._create()
        # Move the worktree onto a different branch: reuse must refuse.
        self._git("checkout", "-q", "-b", "somebody/else", cwd=wt)
        found, path, err = detached_lib.git_worktree_reuse(self.branch, target_repo=self.repo)
        self.assertFalse(found)
        self.assertIn("another branch", err)
        self.assertTrue(wt.exists())
