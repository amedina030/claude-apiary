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

import shlex
import sys
import unittest
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import hook_cmd, to_bash_path

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

    def test_global_launcher_mode_uses_quoted_home(self) -> None:
        """Global launcher mode expands $HOME inside quotes, not a bare ~."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"

        cmd = hook_cmd(Path(script), repo_root=Path(apiary))
        tokens = shlex.split(cmd)

        self.assertEqual(tokens[0], "python")
        # A single, unbroken token — quoting protected it. A bare ~ also could
        # not carry a space/apostrophe, hence $HOME.
        self.assertEqual(tokens[1], "$HOME/.claude/apiary_launch.py")
        self.assertNotIn("~", cmd)

    def test_per_repo_launcher_never_embeds_the_repo_path(self) -> None:
        """Per-repo mode references $CLAUDE_PROJECT_DIR, so a hostile repo
        path never appears in the command at all — the safest design."""
        apiary = HOSTILE_HOME / "claude-apiary"
        script = apiary / "budgeter" / "hooks" / "pre.py"

        cmd = hook_cmd(Path(script), repo_root=Path(apiary), per_repo_launcher=True)
        tokens = shlex.split(cmd)

        self.assertEqual(tokens[1], "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py")
        # The literal hostile path must not be baked into the command.
        self.assertNotIn("Nelson's PC", cmd)

    def test_per_repo_launcher_requires_repo_root(self) -> None:
        with self.assertRaises(ValueError):
            hook_cmd(Path("x.py"), per_repo_launcher=True)


if __name__ == "__main__":
    unittest.main()
