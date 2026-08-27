"""Tests for ``core/hooks_lib.py`` — hook command generation.

Regression coverage for paths that contain a space and/or an apostrophe
(e.g. a Windows home dir like ``C:\\Users\\Nelson's PC``). Such characters
break unquoted shell command strings; the hook command written into
``.claude/settings.json`` must survive them. Each test parses the generated
command with ``shlex`` (posix mode) — an unquoted apostrophe would either
raise (unterminated quote) or split the path into multiple tokens, so a clean
single-token parse proves the quoting holds.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import (
    APIARY_HOOK_MARKER,
    APIARY_PYTHON_ENV,
    hook_cmd,
    is_apiary_entry,
    register_hooks,
    remove_hooks,
    resolve_python,
    to_bash_path,
)

# A home dir that exercises every fragile character at once: a space, an
# apostrophe, and a non-ASCII letter.
HOSTILE_HOME = PureWindowsPath(r"C:\Users\Nelson's PC\Jös")


class HookCmdQuotingTests(unittest.TestCase):
    def test_legacy_absolute_mode_survives_space_and_apostrophe(self) -> None:
        """Legacy absolute-path mode quotes both the interpreter and script."""
        exe = HOSTILE_HOME / "py" / "python.exe"
        script = HOSTILE_HOME / "claude-apiary" / "budgeter" / "hooks" / "pre.py"

        cmd = hook_cmd(Path(script), python_exe=Path(exe))
        # comments=True drops the trailing ownership marker, which is a shell
        # comment — see MarkerIsShellSafeTests.
        tokens = shlex.split(cmd, comments=True)  # raises on a dangling apostrophe

        self.assertEqual(
            tokens,
            [to_bash_path(Path(exe)), to_bash_path(Path(script))],
            msg=f"path with space/apostrophe was not preserved as single tokens: {cmd!r}",
        )

    def test_repo_root_without_per_repo_launcher_is_rejected(self) -> None:
        """The retired global mode (`$HOME/.claude/apiary_launch.py`) must
        not come back by accident: that launcher no longer exists, so a
        command naming it would install a silently broken hook."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"

        with self.assertRaises(ValueError):
            hook_cmd(Path(script), repo_root=Path(apiary))

    def test_per_repo_launcher_never_embeds_the_repo_path(self) -> None:
        """Per-repo mode references $CLAUDE_PROJECT_DIR, so a hostile repo
        path never appears in the command at all — the safest design. The
        interpreter is still a resolved, quoted absolute path (not bare
        `python`/`python3`, neither of which is portable across OSes)."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"
        exe = HOSTILE_HOME / "py" / "python.exe"

        cmd = hook_cmd(
            Path(script), python_exe=Path(exe), repo_root=Path(apiary), per_repo_launcher=True
        )
        tokens = shlex.split(cmd, comments=True)

        self.assertEqual(tokens[0], to_bash_path(Path(exe)))
        self.assertEqual(tokens[1], "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py")
        # The literal hostile *repo* path must not be baked into the command.
        self.assertNotIn("claude-apiary/budgeter", cmd)

    def test_per_repo_launcher_requires_repo_root(self) -> None:
        with self.assertRaises(ValueError):
            hook_cmd(Path("x.py"), per_repo_launcher=True)


class ResolvePythonTests(unittest.TestCase):
    """The single interpreter-resolution choke point (APIARY_PYTHON → current)."""

    def test_defaults_to_running_interpreter(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(APIARY_PYTHON_ENV, None)
            self.assertEqual(resolve_python(), Path(sys.executable))

    def test_env_override_wins(self) -> None:
        override = HOSTILE_HOME / "py" / "python.exe"
        with mock.patch.dict(os.environ, {APIARY_PYTHON_ENV: str(override)}):
            self.assertEqual(resolve_python(), Path(str(override)))

    def test_blank_override_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {APIARY_PYTHON_ENV: "   "}):
            self.assertEqual(resolve_python(), Path(sys.executable))

    def test_hook_cmd_fallback_honors_override(self) -> None:
        """With no explicit python_exe, hook_cmd resolves through the override."""
        override = HOSTILE_HOME / "py" / "python.exe"
        with mock.patch.dict(os.environ, {APIARY_PYTHON_ENV: str(override)}):
            cmd = hook_cmd(Path("x.py"))  # legacy absolute mode, python_exe=None
            tokens = shlex.split(cmd, comments=True)
        self.assertEqual(tokens[0], to_bash_path(Path(str(override))))


def _entry(command: str) -> dict:
    """A settings.json hook entry in the shape Claude Code expects."""
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}


APIARY_DIR = HOSTILE_HOME / "claude-apiary"


class MarkerIsShellSafeTests(unittest.TestCase):
    """The ownership marker rides in the command string, so it must be inert.

    settings.json hook objects are documented as ``{type, command, timeout, …}``
    and the docs say nothing about extra keys, so a ``"_apiary": true`` field
    could be rejected by a stricter reader. A trailing ``#`` comment is safe in
    every shell Claude Code uses for the shell form (``sh -c`` on macOS/Linux,
    Git Bash on Windows, PowerShell when Git Bash is absent) — all three treat
    a space-preceded ``#`` as start-of-comment.
    """

    def test_marker_is_the_tail_of_every_generated_command(self) -> None:
        cmd = hook_cmd(
            Path(APIARY_DIR / "core" / "hooks" / "x.py"),
            python_exe=Path("py"),
            repo_root=Path(APIARY_DIR),
            per_repo_launcher=True,
        )
        self.assertTrue(cmd.endswith(APIARY_HOOK_MARKER), cmd)
        self.assertTrue(APIARY_HOOK_MARKER.startswith(" #"), "the marker must open a shell comment")

    def test_marker_parses_away_as_a_comment(self) -> None:
        cmd = hook_cmd(Path("x.py"), python_exe=Path("py"))
        self.assertNotIn(
            "claude-apiary",
            " ".join(shlex.split(cmd, comments=True)),
            "the marker must not survive as an argument to the hook script",
        )


class IsApiaryEntryTests(unittest.TestCase):
    """Bug 8 — ownership must be an explicit mark, never a path coincidence."""

    def test_a_generated_entry_is_ours(self) -> None:
        cmd = hook_cmd(
            Path(APIARY_DIR / "core" / "hooks" / "x.py"),
            python_exe=Path("py"),
            repo_root=Path(APIARY_DIR),
            per_repo_launcher=True,
        )
        self.assertTrue(is_apiary_entry(_entry(cmd)))

    def test_a_user_hook_naming_a_runner_dir_is_not_ours(self) -> None:
        # The Bug 8 reproduction: a repo's own lint hook happens to live under
        # a directory apiary also ships. It used to be deleted on every install.
        self.assertFalse(is_apiary_entry(_entry("python scripts/runner/lint.py")))

    def test_other_shipped_dir_names_are_not_ours_either(self) -> None:
        for path in (
            "tools/scribe/note.py",
            "hooks/harden/pre.sh",
            "src/refiner/run.py",
            "docs/hooks/build.py",
            "core/hooks/mine.py",
            "budgeter/hooks/mine.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_apiary_entry(_entry(f"python {path}")))

    def test_legacy_global_launcher_entries_are_still_ours(self) -> None:
        # Pre-migration repos must still be cleanable by install/uninstall.
        self.assertTrue(
            is_apiary_entry(_entry('python "$HOME/.claude/apiary_launch.py" core/hooks/x.py'))
        )

    def test_legacy_per_repo_launcher_entries_are_still_ours(self) -> None:
        self.assertTrue(
            is_apiary_entry(
                _entry('python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" core/hooks/x.py')
            )
        )

    def test_legacy_absolute_path_entries_are_still_ours(self) -> None:
        self.assertTrue(is_apiary_entry(_entry('"/d/repos/claude-apiary/core/hooks/x.py"')))


class UserHookSurvivalTests(unittest.TestCase):
    """register_hooks / remove_hooks must only ever touch marked entries."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = Path(self._tmp.name).resolve() / "settings.json"
        self.user_entry = _entry("python scripts/runner/lint.py")
        self.settings.write_text(
            json.dumps({"hooks": {"PreToolUse": [self.user_entry]}}),
            encoding="utf-8",
        )
        self.ours = _entry(
            hook_cmd(
                Path(APIARY_DIR / "core" / "hooks" / "x.py"),
                python_exe=Path("py"),
                repo_root=Path(APIARY_DIR),
                per_repo_launcher=True,
            )
        )

    def _hooks(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]

    def test_register_keeps_the_user_entry(self) -> None:
        register_hooks(self.settings, {"PreToolUse": [self.ours]})
        self.assertIn(self.user_entry, self._hooks()["PreToolUse"])
        self.assertIn(self.ours, self._hooks()["PreToolUse"])

    def test_register_twice_does_not_duplicate_our_entry(self) -> None:
        register_hooks(self.settings, {"PreToolUse": [self.ours]})
        register_hooks(self.settings, {"PreToolUse": [self.ours]})
        entries = self._hooks()["PreToolUse"]
        self.assertEqual(entries.count(self.ours), 1)

    def test_remove_keeps_the_user_entry(self) -> None:
        register_hooks(self.settings, {"PreToolUse": [self.ours]})
        report = remove_hooks(self.settings)
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(self._hooks()["PreToolUse"], [self.user_entry])


if __name__ == "__main__":
    unittest.main()
