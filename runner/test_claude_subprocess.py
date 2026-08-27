#!/usr/bin/env python3
"""Unit tests for runner/claude_subprocess.py env allowlist."""
import unittest
import subprocess
from pathlib import Path
from unittest import mock

from runner.claude_subprocess import (
    ALLOW_ALL_ENV_VAR,
    RUNNER_SUBPROCESS_ENV_VAR,
    _build_subprocess_env,
)


class TestRunClaudeCommand(unittest.TestCase):
    """The argv every runner stage spawns: git push denied, turns capped."""

    def _argv(self, **kwargs):
        from runner import claude_subprocess as cs
        captured = {}

        def fake_run(cmd, **run_kwargs):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"stub")

        with mock.patch.object(cs.subprocess, "run", fake_run):
            rc, _out, _err = cs.run_claude("hello", **kwargs)
        self.assertEqual(rc, 1)
        return captured["cmd"]

    def test_default_denies_git_push_and_caps_turns(self):
        from runner import claude_subprocess as cs
        cmd = self._argv()
        # argv[0] is whatever `which` resolved (a bare "claude" only when the
        # CLI is not on PATH) — see test_resolve_claude_bin below.
        self.assertTrue(
            cmd[0] == "claude" or "claude" in cmd[0].lower(), cmd[0],
        )
        self.assertEqual(cmd[1:5], ["-p", "-", "--output-format", "json"])
        i = cmd.index("--disallowedTools")
        self.assertEqual(tuple(cmd[i + 1:i + 1 + len(cs.DEFAULT_DISALLOWED_TOOLS)]),
                         cs.DEFAULT_DISALLOWED_TOOLS)
        self.assertIn("Bash(git push *)", cmd)
        self.assertNotIn("Bash(git * push *)", cmd)  # denied `git log --grep push`
        j = cmd.index("--max-turns")
        self.assertEqual(cmd[j + 1], str(cs.DEFAULT_MAX_TURNS))

    def test_default_grants_the_stage_tools_explicitly(self):
        # Without the apiary hooks' allow vote (review C-1) a headless claude
        # denies every tool it is not explicitly granted; the runner must
        # bring its own narrow grant.
        from runner import claude_subprocess as cs
        cmd = self._argv()
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        k = cmd.index("--allowedTools")
        grants = cmd[k + 1:k + 1 + len(cs.DEFAULT_ALLOWED_TOOLS)]
        self.assertEqual(tuple(grants), cs.DEFAULT_ALLOWED_TOOLS)
        for must in ("Edit", "Write", "Bash(git *)"):
            self.assertIn(must, grants)
        self.assertNotIn("Bash(git push *)", grants)
        # A deny at any level beats an allow at every other level.
        self.assertLess(cmd.index("--allowedTools"), cmd.index("--disallowedTools"))

    def test_model_and_overrides(self):
        cmd = self._argv(model="sonnet", max_turns=7, disallowed_tools=("Bash(rm *)",))
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "7")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1:], ["Bash(rm *)"])

    def test_none_and_empty_remove_the_flags(self):
        cmd = self._argv(max_turns=None, disallowed_tools=(), allowed_tools=(), permission_mode=None)
        self.assertNotIn("--max-turns", cmd)
        self.assertNotIn("--disallowedTools", cmd)
        self.assertNotIn("--allowedTools", cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_config_overrides_the_defaults(self):
        from runner import claude_subprocess as cs
        table = {("subprocess", "max_turns"): 9, ("subprocess", "allowed_tools"): ["Read"],
                 ("subprocess", "permission_mode"): "plan"}
        with mock.patch.object(cs, "cfg", lambda s, k, d=None: table.get((s, k), d)):
            cmd = self._argv()
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "9")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")

    def test_empty_stderr_on_failure_is_explained_from_the_json(self):
        from runner import claude_subprocess as cs
        envelope = b'{"type":"result","subtype":"error_max_turns","is_error":true,"result":""}'
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout=envelope, stderr=b"")
        with mock.patch.object(cs.subprocess, "run", fake_run):
            rc, out, err = cs.run_claude("hello")
        self.assertEqual(rc, 1)
        self.assertIn("error_max_turns", err)
        self.assertIn("--max-turns", err)
        # Real stderr is never replaced.
        def fake_run2(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout=envelope, stderr=b"real reason")
        with mock.patch.object(cs.subprocess, "run", fake_run2):
            _rc, _out, err = cs.run_claude("hello")
        self.assertEqual(err, "real reason")
        self.assertEqual(cs.describe_failure("not json", 3), "claude exited 3 with no stderr")

    def test_non_positive_max_turns_rejected(self):
        from runner import claude_subprocess as cs
        with self.assertRaises(ValueError):
            cs.run_claude("x", max_turns=0)


class TestBuildSubprocessEnv(unittest.TestCase):
    def test_runner_sentinel_always_set(self):
        env = _build_subprocess_env({}, is_windows=False)
        self.assertEqual(env[RUNNER_SUBPROCESS_ENV_VAR], "1")

    def test_posix_system_vars_forwarded(self):
        parent = {"PATH": "/usr/bin", "HOME": "/home/u", "SHELL": "/bin/bash"}
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/u")
        self.assertEqual(env["SHELL"], "/bin/bash")

    def test_windows_system_vars_forwarded(self):
        parent = {
            "PATH": "C:\\Windows",
            "SYSTEMROOT": "C:\\Windows",
            "APPDATA": "C:\\Users\\u\\AppData\\Roaming",
            "USERPROFILE": "C:\\Users\\u",
        }
        env = _build_subprocess_env(parent, is_windows=True)
        self.assertEqual(env["SYSTEMROOT"], "C:\\Windows")
        self.assertEqual(env["APPDATA"], "C:\\Users\\u\\AppData\\Roaming")
        self.assertEqual(env["USERPROFILE"], "C:\\Users\\u")

    def test_anthropic_prefix_forwarded(self):
        parent = {"ANTHROPIC_API_KEY": "sk-xxx", "CLAUDE_CONFIG_DIR": "/x"}
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-xxx")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x")

    def test_apiary_prefix_forwarded(self):
        parent = {"APIARY_SOMETHING": "yes", "APIARY_OTHER": "no"}
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertEqual(env["APIARY_SOMETHING"], "yes")
        self.assertEqual(env["APIARY_OTHER"], "no")

    def test_proxy_vars_forwarded_both_cases(self):
        parent = {
            "HTTPS_PROXY": "http://proxy:8080",
            "no_proxy": "localhost",
        }
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy:8080")
        self.assertEqual(env["no_proxy"], "localhost")

    def test_sensitive_vars_stripped(self):
        """Core security property: secrets not matching allowlist are dropped."""
        parent = {
            "AWS_SECRET_ACCESS_KEY": "leak-me",
            "GITHUB_TOKEN": "ghp_xxx",
            "SSH_AUTH_SOCK": "/tmp/ssh",
            "GPG_PRIVATE_KEY": "----",
            "DATABASE_PASSWORD": "hunter2",
            "OPENAI_API_KEY": "sk-other",  # apiary:allow-secret
            "PATH": "/usr/bin",
        }
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GPG_PRIVATE_KEY", env)
        self.assertNotIn("DATABASE_PASSWORD", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_escape_hatch_forwards_everything(self):
        parent = {
            ALLOW_ALL_ENV_VAR: "1",
            "AWS_SECRET_ACCESS_KEY": "leak-me",
            "PATH": "/usr/bin",
        }
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "leak-me")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env[RUNNER_SUBPROCESS_ENV_VAR], "1")

    def test_escape_hatch_only_on_literal_one(self):
        """Only the string '1' triggers the escape hatch — not 'true', empty, etc."""
        for val in ("0", "true", "yes", "", "1 "):
            parent = {ALLOW_ALL_ENV_VAR: val, "AWS_SECRET_ACCESS_KEY": "leak"}
            env = _build_subprocess_env(parent, is_windows=False)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env,
                             f"escape hatch incorrectly triggered for {val!r}")

    def test_windows_specific_var_excluded_on_posix(self):
        parent = {"SYSTEMROOT": "C:\\Windows", "HOME": "/home/u"}
        env = _build_subprocess_env(parent, is_windows=False)
        self.assertNotIn("SYSTEMROOT", env)
        self.assertEqual(env["HOME"], "/home/u")

    def test_posix_specific_var_excluded_on_windows(self):
        parent = {"SHELL": "/bin/bash", "SYSTEMROOT": "C:\\Windows"}
        env = _build_subprocess_env(parent, is_windows=True)
        self.assertNotIn("SHELL", env)
        self.assertEqual(env["SYSTEMROOT"], "C:\\Windows")


class TestUsageEmission(unittest.TestCase):
    """review runner Bug 8: `<usage>` used to be emitted only on a zero exit,
    so a call that hit --max-turns or was stopped by the API after real spend
    reported zero tokens — invisible to the budgeter and to the run's cap."""

    def _run(self, returncode, stdout_bytes):
        from runner import claude_subprocess as cs
        emitted = []

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode,
                                               stdout=stdout_bytes, stderr=b"")

        with (
            mock.patch.object(cs.subprocess, "run", fake_run),
            mock.patch.object(cs, "emit_usage_xml", emitted.append),
        ):
            cs.run_claude("hello")
        return emitted

    def _envelope(self):
        import json as _json
        return _json.dumps({
            "result": "stopped",
            "subtype": "error_max_turns",
            "usage": {"input_tokens": 900, "output_tokens": 10},
        }).encode("utf-8")

    def test_usage_emitted_on_success(self):
        self.assertEqual(len(self._run(0, self._envelope())), 1)

    def test_usage_emitted_on_non_zero_exit(self):
        emitted = self._run(1, self._envelope())
        self.assertEqual(len(emitted), 1)
        self.assertIn("input_tokens", emitted[0])

    def test_no_envelope_is_still_a_single_no_op_call(self):
        emitted = self._run(1, b"")
        self.assertEqual(emitted, [""])


class TestResolveClaudeBin(unittest.TestCase):
    """Which executable a stage actually spawns."""

    def test_explicit_override_wins(self):
        from runner import claude_subprocess as cs
        env = {cs.CLAUDE_BIN_ENV_VAR: "/opt/bin/claude-next", "PATH": ""}
        self.assertEqual(cs.resolve_claude_bin(env), "/opt/bin/claude-next")

    def test_falls_back_to_bare_name_when_not_on_path(self):
        from runner import claude_subprocess as cs
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cs.resolve_claude_bin({"PATH": td}), "claude")

    def test_resolves_through_pathext(self):
        """A bare "claude" is resolved by CreateProcess on Windows, which
        only ever appends .exe — an npm-installed claude.cmd is invisible to
        it. `which` honours PATHEXT, so both installs launch."""
        from runner import claude_subprocess as cs
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            name = "claude.bat" if os.name == "nt" else "claude"
            shim = Path(td) / name
            shim.write_text("", encoding="utf-8")
            if os.name != "nt":
                shim.chmod(0o755)
            resolved = cs.resolve_claude_bin({"PATH": td})
            self.assertEqual(Path(resolved).parent.resolve(), Path(td).resolve())


if __name__ == "__main__":
    unittest.main()
