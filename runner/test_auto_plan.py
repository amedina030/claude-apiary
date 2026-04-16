#!/usr/bin/env python3
"""Tests for runner/auto_plan.py build_prompt — phase 4 multi-repo awareness.

Covers the two build_prompt modes introduced in phase 4:
  - target == apiary (or None): byte-identical prompt to the pre-refactor
    output, with the historical apiary-specific instruction block and the
    hardcoded BANNED TOKENS section.
  - target != apiary: generic instruction, target CLAUDE.md injected as
    '## Target repo conventions', banned-tokens section driven by config
    overrides (empty by default for non-apiary targets).
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import auto_plan


def _base_spec() -> dict:
    """Minimal spec fixture — real intake / spec structure isn't verified here,
    we only care about what build_prompt emits."""
    return {
        "id": "abc12345",
        "intake_id": "abc12345",
        "valid": True,
        "acceptance_criteria": ["a thing happens"],
        "files_examined": [],
    }


class TestApiaryBuildPrompt(unittest.TestCase):
    """Apiary target: prompt must contain the historical apiary-specific
    instruction block and the hardcoded BANNED TOKENS section."""

    def test_apiary_default_contains_hard_rules(self):
        prompt = auto_plan.build_prompt(_base_spec())
        # Historical instruction 1 phrasing: readers will recognize it.
        self.assertIn("ALWAYS read CLAUDE.md", prompt)
        self.assertIn("stdlib only (no external dependencies)", prompt)
        self.assertIn("RUNNER-PACKAGE IMPORT CONVENTION", prompt)

    def test_apiary_default_banned_tokens_section(self):
        prompt = auto_plan.build_prompt(_base_spec())
        self.assertIn("## BANNED TOKENS", prompt)
        self.assertIn("- 'pytest' → use 'python -m unittest <module>' instead", prompt)
        self.assertIn("- 'shell=true' → use list-form subprocess args instead", prompt)
        self.assertIn("- 'import requests' / 'from requests' → stdlib only (use urllib)", prompt)

    def test_apiary_explicit_vs_none_are_identical(self):
        """Passing target_repo=<apiary> explicitly must produce the same
        prompt as omitting it — apiary is the default."""
        none_prompt = auto_plan.build_prompt(_base_spec())
        apiary_prompt = auto_plan.build_prompt(
            _base_spec(), target_repo=str(auto_plan.REPO_ROOT),
        )
        self.assertEqual(none_prompt, apiary_prompt)

    def test_apiary_no_target_conventions_injection(self):
        """Apiary case must NOT inject '## Target repo conventions' — the
        planner already references apiary's own CLAUDE.md via the
        instruction text, and a redundant inject breaks byte-identity."""
        prompt = auto_plan.build_prompt(_base_spec())
        self.assertNotIn("## Target repo conventions", prompt)


class TestNonApiaryBuildPrompt(unittest.TestCase):
    """Non-apiary target: generic instruction, target CLAUDE.md injected,
    no hardcoded apiary banned-tokens list unless configured."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name)
        # Minimal git repo — resolve_target_repo isn't called here, but a
        # realistic shape keeps fixtures honest.
        subprocess.run(["git", "init", "--quiet", str(self.target)],
                       check=True, capture_output=True)
        # Fixture CLAUDE.md whose content is obviously NOT apiary's.
        (self.target / "CLAUDE.md").write_text(
            "# Fixture Project\n\nUse pytest. Node conventions. "
            "No stdlib-only rule.\n",
            encoding="utf-8",
        )

    def test_non_apiary_injects_target_claude_md(self):
        prompt = auto_plan.build_prompt(_base_spec(), target_repo=str(self.target))
        self.assertIn("## Target repo conventions", prompt)
        self.assertIn("# Fixture Project", prompt)
        self.assertIn("Use pytest. Node conventions.", prompt)

    def test_non_apiary_drops_apiary_instruction_block(self):
        """The hardcoded apiary-specific guidance must NOT leak into a
        non-apiary prompt — that's the whole point of phase 4."""
        prompt = auto_plan.build_prompt(_base_spec(), target_repo=str(self.target))
        self.assertNotIn("ALWAYS read CLAUDE.md", prompt)
        self.assertNotIn("stdlib only (no external dependencies)", prompt)
        self.assertNotIn("RUNNER-PACKAGE IMPORT CONVENTION", prompt)

    def test_non_apiary_no_banned_tokens_section_by_default(self):
        """With no per-target override configured, a non-apiary prompt
        omits the BANNED TOKENS section entirely."""
        prompt = auto_plan.build_prompt(_base_spec(), target_repo=str(self.target))
        self.assertNotIn("## BANNED TOKENS", prompt)
        self.assertNotIn("'pytest'", prompt)

    def test_non_apiary_with_override_renders_banned_section(self):
        """A per-target override populates BANNED TOKENS from config."""
        key = self.target.name
        fake_cfg = {
            "runner": {
                "target_overrides": {
                    key: {"banned_tokens": {"console.log": "no debug prints in prod"}}
                },
            },
        }

        def _cfg(section, field, default=None):
            return fake_cfg.get(section, {}).get(field, default)

        with mock.patch("runner.validate_plan.cfg", side_effect=_cfg):
            prompt = auto_plan.build_prompt(
                _base_spec(), target_repo=str(self.target),
            )
        self.assertIn("## BANNED TOKENS", prompt)
        self.assertIn("- 'console.log' → no debug prints in prod", prompt)

    def test_non_apiary_missing_claude_md_omits_section(self):
        """If the target has no CLAUDE.md, the conventions heading does not
        appear as its own section (generic instruction 1 legitimately mentions
        the section name as a pointer to where conventions *would* live)."""
        (self.target / "CLAUDE.md").unlink()
        prompt = auto_plan.build_prompt(_base_spec(), target_repo=str(self.target))
        # No CLAUDE.md body was injected: the fixture's marker is absent.
        self.assertNotIn("# Fixture Project", prompt)
        # And no heading line appears as its own section.
        self.assertNotIn("\n## Target repo conventions\n", prompt)


class TestResolveBannedTokens(unittest.TestCase):
    """Phase 4: validate_plan._resolve_banned_tokens precedence rules."""

    def test_apiary_target_gets_default_list(self):
        from runner.validate_plan import _resolve_banned_tokens
        tokens = _resolve_banned_tokens(str(auto_plan.REPO_ROOT))
        self.assertIn("pytest", tokens)
        self.assertIn("shell=true", tokens)

    def test_none_target_gets_default_list(self):
        from runner.validate_plan import _resolve_banned_tokens
        tokens = _resolve_banned_tokens(None)
        self.assertIn("pytest", tokens)

    def test_non_apiary_default_is_empty(self):
        """Non-apiary target with no config override defaults to {}."""
        from runner.validate_plan import _resolve_banned_tokens
        with tempfile.TemporaryDirectory() as td:
            tokens = _resolve_banned_tokens(td)
            self.assertEqual(tokens, {})

    def test_per_target_override_wins(self):
        """runner.target_overrides.<basename>.banned_tokens overrides both
        apiary default and empty fallback."""
        from runner.validate_plan import _resolve_banned_tokens
        with tempfile.TemporaryDirectory() as td:
            key = Path(td).name
            fake_cfg = {
                "runner": {
                    "target_overrides": {
                        key: {"banned_tokens": {"foo": "bar"}}
                    },
                },
            }

            def _cfg(section, field, default=None):
                return fake_cfg.get(section, {}).get(field, default)

            with mock.patch("runner.validate_plan.cfg", side_effect=_cfg):
                tokens = _resolve_banned_tokens(td)
            self.assertEqual(tokens, {"foo": "bar"})


class TestValidatePlanRespectsTargetRepo(unittest.TestCase):
    """Phase 4: validate() pulls banned_tokens from the plan's target_repo
    field so a non-apiary plan mentioning 'pytest' isn't falsely rejected."""

    def _plan_with_pytest(self, target_repo: str | None) -> dict:
        plan = {
            "uuid": "x",
            "executor_model": "sonnet",
            "spec": {"acceptance_criteria": []},
            "steps": [{
                "step_number": 1,
                "type": "test",
                "description": "Run pytest test_app.py",
                "action": "test",
                "files": ["tests/test_app.py"],
                "depends_on": [],
                "code_spec": "pytest tests/test_app.py",
            }],
        }
        if target_repo:
            plan["target_repo"] = target_repo
        return plan

    def test_apiary_plan_with_pytest_still_rejected(self):
        """Apiary self-runs must keep rejecting pytest — regression guard."""
        from runner.validate_plan import validate
        errors = validate(self._plan_with_pytest(target_repo=None))
        self.assertTrue(any("pytest" in e for e in errors))

    def test_non_apiary_plan_with_pytest_accepted(self):
        """Non-apiary target defaults to empty banned-tokens → pytest allowed."""
        from runner.validate_plan import validate
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init", "--quiet", td],
                           check=True, capture_output=True)
            errors = validate(self._plan_with_pytest(target_repo=td))
            self.assertFalse(any("banned token 'pytest'" in e for e in errors),
                             f"non-apiary plan should not reject pytest, got: {errors}")


if __name__ == "__main__":
    unittest.main()
