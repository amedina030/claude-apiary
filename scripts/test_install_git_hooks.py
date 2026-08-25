#!/usr/bin/env python3
"""Tests for scripts/install_git_hooks.py.

Includes a genuine end-to-end case: a real ``git commit`` in a throwaway repo
with the hook installed, so the acceptance criterion "staging a fake key and
committing is blocked" is proven rather than assumed.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import install_git_hooks  # noqa: E402

FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"

# A stand-in for the per-repo launcher. The real one resolves main-apiary from
# a pointer file; here it just reports this checkout, which is what the hook
# needs in order to locate scripts/secret_scan.py.
LAUNCHER_STUB = """import sys
if len(sys.argv) >= 2 and sys.argv[1] == "--print-repo-path":
    print(r"{root}")
    raise SystemExit(0)
raise SystemExit(2)
"""


def _git(args, cwd, **kw):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, **kw
    )


class _TempRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(["init", "-q"], self.repo)
        _git(["config", "user.email", "t@example.com"], self.repo)
        _git(["config", "user.name", "T"], self.repo)
        _git(["config", "commit.gpgsign", "false"], self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def hook(self) -> Path:
        return self.repo / ".git" / "hooks" / "pre-commit"


class InstallTests(_TempRepo):
    def test_install_creates_the_hook(self):
        rc = install_git_hooks.main(["--repo", str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertTrue(self.hook.is_file())
        self.assertIn(install_git_hooks.OWNED_MARKER, self.hook.read_text(encoding="utf-8"))

    def test_install_is_idempotent(self):
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo)]), 0)
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo)]), 0)
        self.assertTrue(self.hook.is_file())

    def test_foreign_hook_is_not_clobbered(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        rc = install_git_hooks.main(["--repo", str(self.repo)])
        self.assertEqual(rc, 1)
        self.assertIn("echo mine", self.hook.read_text(encoding="utf-8"))

    def test_force_replaces_a_foreign_hook(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        rc = install_git_hooks.main(["--repo", str(self.repo), "--force"])
        self.assertEqual(rc, 0)
        self.assertIn(install_git_hooks.OWNED_MARKER, self.hook.read_text(encoding="utf-8"))

    def test_refuses_to_target_main_apiary(self):
        rc = install_git_hooks.main(["--repo", str(REPO_ROOT)])
        self.assertEqual(rc, 1)

    def test_non_git_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(install_git_hooks.main(["--repo", plain]), 1)


class HooksPathTests(_TempRepo):
    """core.hooksPath overrides .git/hooks entirely.

    Regression for a live failure: this repo carried a stale core.hooksPath
    from a directory rename, pointing somewhere that no longer existed, so
    every git hook was inert while the installer still reported success.
    """

    def test_unset_hookspath_uses_git_hooks(self):
        target, warning = install_git_hooks.hooks_dir(self.repo)
        self.assertEqual(target, self.repo / ".git" / "hooks")
        self.assertIsNone(warning)

    def test_redirect_installs_where_git_actually_looks(self):
        elsewhere = self.repo / "custom-hooks"
        elsewhere.mkdir()
        _git(["config", "core.hooksPath", str(elsewhere)], self.repo)
        rc = install_git_hooks.main(["--repo", str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertTrue((elsewhere / "pre-commit").is_file())
        # Nothing written to the directory git is ignoring.
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-commit").exists())

    def test_dangling_redirect_is_reported(self):
        missing = self.repo / "nope" / "hooks"
        _git(["config", "core.hooksPath", str(missing)], self.repo)
        _, warning = install_git_hooks.hooks_dir(self.repo)
        self.assertIsNotNone(warning)
        self.assertIn("does not exist", warning)
        self.assertIn("git config --unset core.hooksPath", warning)


class UninstallTests(_TempRepo):
    def test_uninstall_removes_our_hook(self):
        install_git_hooks.main(["--repo", str(self.repo)])
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo), "--uninstall"]), 0)
        self.assertFalse(self.hook.exists())

    def test_uninstall_leaves_a_foreign_hook_alone(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo), "--uninstall"]), 1)
        self.assertTrue(self.hook.exists())

    def test_uninstall_with_nothing_installed_is_fine(self):
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo), "--uninstall"]), 0)

    def test_list_reports_without_changing(self):
        self.assertEqual(install_git_hooks.main(["--repo", str(self.repo), "--list"]), 0)
        self.assertFalse(self.hook.exists())


@unittest.skipIf(shutil.which("git") is None, "git not on PATH")
class EndToEndCommitTests(_TempRepo):
    """The acceptance criteria, exercised through real `git commit` runs."""

    def setUp(self):
        super().setUp()
        launcher = self.repo / ".claude" / "apiary" / "launch.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(LAUNCHER_STUB.format(root=REPO_ROOT), encoding="utf-8")
        install_git_hooks.main(["--repo", str(self.repo)])

    def _commit(self, message="wip"):
        return _git(["commit", "-m", message], self.repo)

    def _write(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_clean_commit_succeeds(self):
        self._write("ok.py", "print('hi')\n")
        _git(["add", "ok.py"], self.repo)
        proc = self._commit()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commit_with_a_key_is_blocked(self):
        self._write("cfg.py", f"aws = '{FAKE_AWS_KEY}'\n")
        _git(["add", "cfg.py"], self.repo)
        proc = self._commit()
        self.assertNotEqual(proc.returncode, 0, "commit should have been blocked")
        combined = proc.stdout + proc.stderr
        self.assertIn("cfg.py", combined)
        self.assertIn("aws-access-key", combined)
        # Nothing was committed.
        self.assertEqual(_git(["rev-list", "-n", "1", "--all"], self.repo).stdout.strip(), "")

    def test_no_verify_bypasses(self):
        self._write("cfg.py", f"aws = '{FAKE_AWS_KEY}'\n")
        _git(["add", "cfg.py"], self.repo)
        proc = _git(["commit", "--no-verify", "-m", "bypass"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_inline_pragma_lets_the_commit_through(self):
        self._write("cfg.py", f"aws = '{FAKE_AWS_KEY}'  # apiary:allow-secret\n")
        _git(["add", "cfg.py"], self.repo)
        proc = self._commit()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_repo_allowlist_lets_the_commit_through(self):
        self._write(".secretsallow", "^fixtures/\n")
        self._write("fixtures/sample.txt", FAKE_AWS_KEY + "\n")
        _git(["add", ".secretsallow", "fixtures/sample.txt"], self.repo)
        proc = self._commit()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_force_added_dotenv_is_blocked(self):
        self._write(".gitignore", ".env\n")
        self._write(".env", "TOKEN=abc\n")
        _git(["add", ".gitignore"], self.repo)
        _git(["add", "-f", ".env"], self.repo)
        proc = self._commit()
        self.assertNotEqual(proc.returncode, 0, "force-added .env should be blocked")
        self.assertIn("blocked-file", proc.stdout + proc.stderr)

    def test_fails_closed_when_main_apiary_is_unreachable(self):
        # A security control that quietly stops working is worse than one that
        # is loudly broken: with no launcher, the commit must NOT sail through.
        shutil.rmtree(self.repo / ".claude")
        self._write("cfg.py", f"aws = '{FAKE_AWS_KEY}'\n")
        _git(["add", "cfg.py"], self.repo)
        proc = self._commit()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--no-verify", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
