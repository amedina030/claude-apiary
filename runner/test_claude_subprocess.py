#!/usr/bin/env python3
"""Unit tests for runner/claude_subprocess.py env allowlist."""
import unittest
import subprocess
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
        self.assertEqual(cmd[:5], ["claude", "-p", "-", "--output-format", "json"])
        i = cmd.index("--disallowedTools")
        self.assertEqual(tuple(cmd[i + 1:i + 1 + len(cs.DEFAULT_DISALLOWED_TOOLS)]),
                         cs.DEFAULT_DISALLOWED_TOOLS)
        self.assertIn("Bash(git push *)", cmd)
        j = cmd.index("--max-turns")
        self.assertEqual(cmd[j + 1], str(cs.DEFAULT_MAX_TURNS))

    def test_model_and_overrides(self):
        cmd = self._argv(model="sonnet", max_turns=7, disallowed_tools=("Bash(rm *)",))
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "7")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1:], ["Bash(rm *)"])

    def test_none_and_empty_remove_the_flags(self):
        cmd = self._argv(max_turns=None, disallowed_tools=())
        self.assertNotIn("--max-turns", cmd)
        self.assertNotIn("--disallowedTools", cmd)

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


if __name__ == "__main__":
    unittest.main()
