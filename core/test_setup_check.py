#!/usr/bin/env python3
"""Tests for setup.py helpers — specifically the run_check() drift detection
that was reporting false negatives before #227.

Two helpers under test:
  - _is_apiary_entry(entry): recognizes both absolute-path and portable
    $CLAUDE_PROJECT_DIR-form hook entries as ours.
  - _expand_hook_script_path(s): resolves $CLAUDE_PROJECT_DIR, env vars,
    ~, and bash-style /c/Users/... paths to a real filesystem Path.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import setup as apiary_setup  # noqa: E402


class TestIsApiaryEntry(unittest.TestCase):
    def test_absolute_path_with_marker(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "/c/Users/foo/claude-apiary/budgeter/hooks/pre_tool_use.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_portable_claude_project_dir_form(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python $CLAUDE_PROJECT_DIR/budgeter/hooks/pre_tool_use.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_core_hooks_subpath(self):
        entry = {
            "matcher": "",
            "hooks": [{"type": "command", "command": "python $CLAUDE_PROJECT_DIR/core/hooks/inject_session.py"}],
        }
        self.assertTrue(apiary_setup._is_apiary_entry(entry))

    def test_third_party_hook_not_recognized(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python /opt/some-other-tool/hooks/foo.py"}],
        }
        self.assertFalse(apiary_setup._is_apiary_entry(entry))

    def test_empty_entry(self):
        self.assertFalse(apiary_setup._is_apiary_entry({}))


class TestExpandHookScriptPath(unittest.TestCase):
    def test_claude_project_dir_resolves_to_repo_root(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/setup.py")
        self.assertTrue(p.exists())
        self.assertEqual(p.resolve(), (REPO_ROOT / "setup.py").resolve())

    def test_known_apiary_hook_resolves(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/core/hooks/inject_session.py")
        self.assertTrue(p.exists())

    def test_nonexistent_path_returns_path(self):
        p = apiary_setup._expand_hook_script_path("$CLAUDE_PROJECT_DIR/no_such_file.py")
        self.assertFalse(p.exists())  # but no exception

    def test_env_var_expansion(self):
        os.environ["_APIARY_TEST_VAR"] = str(REPO_ROOT)
        try:
            p = apiary_setup._expand_hook_script_path("$_APIARY_TEST_VAR/setup.py")
            self.assertTrue(p.exists())
        finally:
            del os.environ["_APIARY_TEST_VAR"]

    def test_bash_style_drive_letter_conversion(self):
        # Build a /<drive>/... form for the repo root and verify it resolves.
        win_path = str(REPO_ROOT / "setup.py")
        # Only run on Windows-style paths (drive letter present).
        if len(win_path) >= 2 and win_path[1] == ":":
            drive = win_path[0].lower()
            rest = win_path[2:].replace("\\", "/")
            bash_path = f"/{drive}{rest}"
            p = apiary_setup._expand_hook_script_path(bash_path)
            self.assertTrue(p.exists(), f"failed for bash_path={bash_path!r} → {p!r}")


class TestDriftDetection(unittest.TestCase):
    """Tests for the [Drift] section of run_check()."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.claude_dir = self.tmpdir / '.claude'
        self.claude_dir.mkdir()
        (self.claude_dir / 'commands').mkdir()
        # Install all enumerated files by copying source -> installed.
        for source, installed, _label in apiary_setup._enumerate_installed_files(self.claude_dir):
            if not source.exists():
                continue
            # Skip git hooks dir since it lives under the repo (.git/hooks), not under claude_dir.
            if '.git' in installed.parts:
                continue
            installed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, installed)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_check_capture(self):
        """Run run_check() with Path.home() redirected to tmpdir.

        The [Runtime] section spawns subprocesses for every hook script and
        calls importlib.import_module(), producing environment-sensitive side
        effects that have nothing to do with drift.  Patch both out so drift
        unit tests stay fast and deterministic (ATK-003).
        """
        buf = io.StringIO()
        mock_proc = mock.Mock()
        mock_proc.returncode = 0
        with mock.patch.object(Path, 'home', return_value=self.tmpdir), \
             mock.patch('subprocess.run', return_value=mock_proc), \
             mock.patch('importlib.import_module'), \
             redirect_stdout(buf):
            try:
                apiary_setup.run_check()
            except SystemExit:
                pass
        return buf.getvalue()

    def _extract_section(self, output, section_header):
        """Return the body lines of a named [Section] from run_check() output."""
        lines = output.splitlines()
        out = []
        in_section = False
        for line in lines:
            if line.strip() == section_header:
                in_section = True
                continue
            if in_section:
                if line.startswith('[') and line.endswith(']'):
                    break
                out.append(line)
        return '\n'.join(out)

    def _drift_section(self, output):
        return self._extract_section(output, '[Drift]')

    def test_clean_install_reports_no_drift(self):
        output = self._run_check_capture()
        drift = self._drift_section(output)
        self.assertNotIn('FAIL', drift)
        self.assertIn('OK', drift)

    def test_edited_installed_file_reports_drift(self):
        # Find a command file that was copied into the temp claude_dir.
        cmd_files = list((self.claude_dir / 'commands').glob('*.md'))
        self.assertTrue(cmd_files, 'expected at least one installed command')
        victim = cmd_files[0]
        victim.write_text(victim.read_text(encoding='utf-8') + '\nDRIFTED\n', encoding='utf-8')
        output = self._run_check_capture()
        drift = self._drift_section(output)
        self.assertIn('FAIL', drift)
        self.assertIn(str(victim), drift)
        self.assertIn('drift between', drift)

    def test_revert_reports_clean(self):
        # Use bytes-mode I/O: text-mode write_text on Windows translates
        # \n → \r\n, so a read_text/write_text round-trip corrupts line
        # endings and the revert would leave bytes differing from source.
        cmd_files = list((self.claude_dir / 'commands').glob('*.md'))
        victim = cmd_files[0]
        original = victim.read_bytes()
        victim.write_bytes(original + b'\nDRIFTED\n')
        drifted = self._drift_section(self._run_check_capture())
        self.assertIn('FAIL', drifted)
        victim.write_bytes(original)
        clean = self._drift_section(self._run_check_capture())
        # FAIL line for the victim specifically should be gone.
        for line in clean.splitlines():
            if 'FAIL' in line:
                self.assertNotIn(str(victim), line)

    def test_missing_installed_file_not_reported_as_drift(self):
        cmd_files = list((self.claude_dir / 'commands').glob('*.md'))
        victim = cmd_files[0]
        victim.unlink()
        drift = self._drift_section(self._run_check_capture())
        # The drift section must not flag this file under any wording
        # (e.g. 'drift between', 'installed missing', etc.) — presence
        # is handled by other sections (ATK-005: enforce the 'skip silently'
        # contract regardless of message phrasing).
        for line in drift.splitlines():
            if 'FAIL' in line:
                self.assertNotIn(victim.name, line)

    def test_launcher_drift_flagged(self):
        # Launcher drift is reported by [Hook launcher], not [Drift].
        # [Drift] intentionally excludes the launcher to avoid double-counting
        # the same failure (ATK-004).  Seed a launcher that matches the source
        # so the hash check starts clean, then corrupt it.
        launcher_source = apiary_setup.CORE_DIR / 'apiary_launch.py'
        if not launcher_source.exists():
            self.skipTest('launcher source missing from repo')
        launcher = self.claude_dir / 'apiary_launch.py'
        import shutil as _shutil
        _shutil.copy2(launcher_source, launcher)
        launcher.write_text(launcher.read_text(encoding='utf-8') + '\n# DRIFT\n', encoding='utf-8')
        output = self._run_check_capture()
        hook_launcher = self._extract_section(output, '[Hook launcher]')
        self.assertIn('FAIL', hook_launcher)
        self.assertIn('apiary_launch.py', hook_launcher)


if __name__ == "__main__":
    unittest.main()
