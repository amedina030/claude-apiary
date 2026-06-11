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

import os
import shlex
import sys
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import APIARY_PYTHON_ENV, hook_cmd, resolve_python, to_bash_path

# A home dir that exercises every fragile character at once: a space, an
# apostrophe, and a non-ASCII letter.
HOSTILE_HOME = PureWindowsPath(r"C:\Users\Nelson's PC\Jös")


class HookCmdQuotingTests(unittest.TestCase):
    def test_legacy_absolute_mode_survives_space_and_apostrophe(self) -> None:
        """Legacy absolute-path mode quotes both the interpreter and script."""
        exe = HOSTILE_HOME / "py" / "python.exe"
        script = HOSTILE_HOME / "claude-apiary" / "budgeter" / "hooks" / "pre.py"

        cmd = hook_cmd(Path(script), python_exe=Path(exe))
        tokens = shlex.split(cmd)  # raises on a dangling apostrophe

        self.assertEqual(
            tokens,
            [to_bash_path(Path(exe)), to_bash_path(Path(script))],
            msg=f"path with space/apostrophe was not preserved as single tokens: {cmd!r}",
        )

    def test_global_launcher_mode_embeds_quoted_interpreter_and_home(self) -> None:
        """Global launcher mode embeds the resolved interpreter (quoted) and
        expands $HOME inside quotes, not a bare ~."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"
        exe = HOSTILE_HOME / "py" / "python.exe"

        cmd = hook_cmd(Path(script), python_exe=Path(exe), repo_root=Path(apiary))
        tokens = shlex.split(cmd)

        # The interpreter is a quoted, bash-converted absolute path — never a
        # bare `python` (absent on macOS Homebrew) nor `python3` (absent on a
        # stock Windows install). Survives the space/apostrophe in the path.
        self.assertEqual(tokens[0], to_bash_path(Path(exe)))
        # A single, unbroken token — quoting protected it. A bare ~ also could
        # not carry a space/apostrophe, hence $HOME.
        self.assertEqual(tokens[1], "$HOME/.claude/apiary_launch.py")
        self.assertNotIn("~", cmd)

    def test_per_repo_launcher_never_embeds_the_repo_path(self) -> None:
        """Per-repo mode references $CLAUDE_PROJECT_DIR, so a hostile repo
        path never appears in the command at all — the safest design. The
        interpreter is still a resolved, quoted absolute path (not bare
        `python`/`python3`, neither of which is portable across OSes)."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"
        exe = HOSTILE_HOME / "py" / "python.exe"

        cmd = hook_cmd(Path(script), python_exe=Path(exe),
                       repo_root=Path(apiary), per_repo_launcher=True)
        tokens = shlex.split(cmd)

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
            tokens = shlex.split(cmd)
        self.assertEqual(tokens[0], to_bash_path(Path(str(override))))


if __name__ == "__main__":
    unittest.main()
