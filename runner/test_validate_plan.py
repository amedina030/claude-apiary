#!/usr/bin/env python3
"""Tests for runner/validate_plan.py.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import validate_plan
from runner.validate_plan import (
    _check_banned_tokens as _check_banned_tokens_impl,
)
from runner.validate_plan import (
    _check_criteria_coverage,
    _check_file_overlap,
    _check_gitignored_paths,
    _check_path_allowlist,
    _check_test_code_spec_format,
    _check_test_failure_language,
    _check_test_shell_metacharacters,
    _criterion_bigrams,
    _resolve_banned_tokens,
    validate,
)

# Phase 4: _check_banned_tokens now takes the banned-tokens dict explicitly.
# Existing tests assert apiary-default behavior, so wrap with the apiary map
# resolved from config (target_repo=None => apiary fallback).
_APIARY_BANNED = _resolve_banned_tokens(None)


def _check_banned_tokens(steps):
    """Back-compat shim: apply the apiary-default banned-tokens map."""
    return _check_banned_tokens_impl(steps, _APIARY_BANNED)


def _base_plan(steps):
    """Wrap a list of steps into a minimally valid plan structure."""
    return {
        "uuid": "test-uuid",
        "executor_model": "sonnet",
        "spec": {"acceptance_criteria": []},
        "steps": steps,
    }


def _step(num, action, code_spec, description="desc", files=None, depends_on=None):
    return {
        "step_number": num,
        "type": action,
        "description": description,
        "action": action,
        "files": files if files is not None else [],
        "depends_on": depends_on if depends_on is not None else [],
        "code_spec": code_spec,
    }


class TestTestCodeSpecFormat(unittest.TestCase):
    """The validator must reject prose code_spec on test-action steps because
    the executor passes it directly to subprocess.run(shell=True). T4 step 6
    hit this exactly with 'Run python -m pytest ...'."""

    def test_single_command_passes(self):
        steps = [_step(1, "test", "python -m unittest runner.test_foo")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_prose_starter_run_rejected(self):
        steps = [_step(1, "test", "Run python -m unittest runner.test_foo")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("prose word 'run'", errors[0])

    def test_prose_starter_execute_rejected(self):
        steps = [_step(1, "test", "Execute the test suite")]
        self.assertEqual(len(_check_test_code_spec_format(steps)), 1)

    def test_multiline_prose_rejected(self):
        steps = [_step(1, "test", "Run the unit tests:\npython -m unittest")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("single shell command on one line", errors[0])

    def test_non_test_action_ignored(self):
        # 'Run' would be flagged as prose, but only test actions get checked
        steps = [_step(1, "create", "Run something — this is freeform pseudocode")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_empty_code_spec_skipped(self):
        # Empty is caught by the required-field check, not this one
        steps = [_step(1, "test", "")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_punctuation_after_first_word_still_rejected(self):
        steps = [_step(1, "test", "Run: python -m unittest")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'run'", errors[0])

    def test_full_validate_surfaces_format_errors(self):
        # Confirm the new check is wired into validate()
        plan = _base_plan(
            [
                _step(1, "create", "Make a file", files=["runner/new_file.py"]),
                _step(2, "test", "Run the tests"),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("prose word" in e for e in errors), f"expected prose-word error in {errors}"
        )


class TestTestFailureLanguage(unittest.TestCase):
    """The validator must reject test-action steps whose description signals
    expected failure. The executor treats every test step as a hard pass/fail
    gate, so a 'this run is expected to report violations' step always aborts.
    Caught in T5b plan step 3 (#211)."""

    def test_clean_test_step_passes(self):
        steps = [
            _step(
                1,
                "test",
                "python -m unittest runner.test_foo",
                description="Verify the new helper passes its unit tests",
            )
        ]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_expected_to_fail_rejected(self):
        steps = [
            _step(
                1,
                "test",
                "python audit.py",
                description="Run the audit (expected to fail before fix)",
            )
        ]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected to fail", errors[0])

    def test_expected_to_report_violations_rejected(self):
        steps = [
            _step(
                1,
                "test",
                "python audit.py",
                description="Run audit (this run is expected to report violations; "
                "it gates subsequent fix steps)",
            )
        ]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        # Both phrases match — the function returns on first match.
        self.assertTrue(
            "expected to report violations" in errors[0] or "this run is expected to" in errors[0]
        )

    def test_should_fail_rejected(self):
        steps = [
            _step(
                1,
                "test",
                "python check.py",
                description="The pre-fix snapshot — should fail until step 4 lands",
            )
        ]
        self.assertEqual(len(_check_test_failure_language(steps)), 1)

    def test_non_test_action_ignored(self):
        steps = [
            _step(
                1, "create", "make file", description="this run is expected to fail before the fix"
            )
        ]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_full_validate_surfaces_failure_language(self):
        plan = _base_plan(
            [
                _step(1, "create", "Add helper", files=["runner/new_file.py"]),
                _step(
                    2,
                    "test",
                    "python audit.py",
                    description="Run the audit script (expected to report violations; "
                    "gates subsequent fix steps)",
                ),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("expected" in e and "test" in e for e in errors),
            f"expected failure-language error in {errors}",
        )


class TestBannedTokens(unittest.TestCase):
    """The validator must reject plans that propose pytest, shell=True, or
    external imports — all hard rule violations per docs/standards/code-style.md.
    T4 step 5 hit pytest exactly: the planner wrote a pytest test file in a
    codebase that mandates unittest stdlib only."""

    def test_clean_plan_passes(self):
        steps = [_step(1, "create", "Add a new helper function")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_pytest_in_code_spec_rejected(self):
        steps = [_step(1, "create", "Use pytest fixtures to set up the test")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'pytest'", errors[0])
        self.assertIn("unittest", errors[0])

    def test_pytest_in_description_rejected(self):
        steps = [
            _step(
                1,
                "create",
                "freeform pseudocode here",
                description="Add pytest tests for the new module",
            )
        ]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)

    def test_shell_true_rejected(self):
        steps = [_step(1, "create", "Call subprocess.run(cmd, shell=True)")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'shell=true'", errors[0])

    def test_import_requests_rejected(self):
        steps = [_step(1, "create", "import requests\nrequests.get(url)")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("stdlib only", errors[0])

    def test_from_requests_rejected(self):
        steps = [_step(1, "create", "from requests import Session")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_case_insensitive(self):
        # Banned tokens match case-insensitively
        steps = [_step(1, "create", "Use PyTest for the suite")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_multiple_violations_all_reported(self):
        steps = [_step(1, "create", "Use pytest with shell=True calls")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 2)

    # #239: false-positive regression tests. Substring match previously
    # rejected any identifier containing a banned token (requests_mock,
    # requests_toolbelt, pytest_fixture, etc.). Word-boundary match
    # should accept these while still catching the real violations.

    def test_requests_mock_import_accepted(self):
        steps = [_step(1, "create", "from requests_mock import Mocker")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_requests_toolbelt_import_accepted(self):
        steps = [_step(1, "create", "import requests_toolbelt as rt")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_pytest_suffix_identifier_accepted(self):
        # A variable or function name that happens to start with 'pytest'
        # but is suffixed (pytest_fixture, pytestify) — should NOT match
        # because word-boundary requires non-word chars on both sides.
        steps = [_step(1, "create", "def pytest_fixture_helper(): pass")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_pytest_hyphenated_still_rejected(self):
        # A hyphen IS a word boundary, so 'pytest-asyncio' correctly
        # triggers the ban — we DO want to flag any form of pytest usage.
        steps = [_step(1, "create", "pip install pytest-asyncio")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_pre_existing_pytest_word_still_rejected(self):
        # Sanity: the straightforward 'pytest' word with whitespace
        # around it must still be caught after the word-boundary rewrite.
        steps = [_step(1, "create", "Run pytest to check the suite")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_verify_step_skips_banned_token_check(self):
        # Verify steps are natural-language checklists that legitimately name
        # banned tokens in negation ("no pytest", "no shell=True").
        steps = [_step(1, "verify", "No pytest. No shell=True. Stdlib only.")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_create_step_with_same_text_still_rejected(self):
        # Same text in a create step SHOULD still be caught.
        steps = [_step(1, "create", "No pytest. No shell=True. Stdlib only.")]
        self.assertEqual(len(_check_banned_tokens(steps)), 2)

    def test_full_validate_surfaces_banned_token_errors(self):
        # Confirm the new check is wired into validate()
        plan = _base_plan(
            [
                _step(1, "create", "Write a pytest test file", files=["runner/test_new.py"]),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("banned token" in e for e in errors), f"expected banned-token error in {errors}"
        )


class TestShellMetacharacters(unittest.TestCase):
    """Reject shell metacharacters in test-action code_spec."""

    def test_semicolon_rejected(self):
        steps = [_step(1, "test", "python -m unittest; rm -rf /")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("';'", errors[0])

    def test_pipe_rejected(self):
        steps = [_step(1, "test", "python -m unittest | tee out.txt")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'|'", errors[0])

    def test_ampersand_rejected(self):
        steps = [_step(1, "test", "python test.py && echo done")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'&'", errors[0])

    def test_backtick_rejected(self):
        steps = [_step(1, "test", "python `which python` -m unittest")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'`'", errors[0])

    def test_dollar_paren_rejected(self):
        steps = [_step(1, "test", "python $(echo test.py)")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'$('", errors[0])

    def test_redirect_rejected(self):
        steps = [_step(1, "test", "python -m unittest > /dev/null")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'>'", errors[0])

    def test_clean_code_spec_passes(self):
        steps = [_step(1, "test", "python -m unittest discover -s runner -p test_validate_plan.py")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 0)

    def test_non_test_action_ignored(self):
        steps = [_step(1, "create", "echo foo > bar.txt")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 0)

    def test_one_error_per_step(self):
        steps = [_step(1, "test", "python test.py; echo done | cat")]
        errors = _check_test_shell_metacharacters(steps)
        self.assertEqual(len(errors), 1)

    def test_integration_with_validate(self):
        plan = _base_plan(
            [
                _step(
                    1,
                    "test",
                    "python -m unittest; rm -rf /",
                    files=["runner/test_validate_plan.py"],
                ),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("metacharacter" in e for e in errors), f"expected metacharacter error in {errors}"
        )


class TestPathAllowlist(unittest.TestCase):
    """#212: every path in step.files (relative or absolute) must resolve
    under the repo working tree. Out-of-repo paths are rejected outright,
    even legitimate state-dir locations — those must be hand-fixed.
    Catches both T5b's absolute Windows paths and `../etc/passwd`
    style traversal in relative paths."""

    def setUp(self):
        # A scratch directory that's guaranteed not to be inside the
        # repo, used as the "rejected" location.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outside_root = Path(self._tmp.name).resolve() / "outside"
        self.outside_root.mkdir(parents=True)

    def test_relative_in_repo_path_accepted(self):
        steps = [_step(1, "create", "x", files=["runner/foo.py"])]
        self.assertEqual(_check_path_allowlist(steps), [])

    def test_absolute_path_under_repo_accepted(self):
        in_repo = validate_plan._REPO_ROOT / "runner" / "foo.py"
        steps = [_step(1, "create", "x", files=[str(in_repo)])]
        self.assertEqual(_check_path_allowlist(steps), [])

    def test_absolute_path_outside_repo_rejected(self):
        bad = str(self.outside_root / "random.json")
        steps = [_step(1, "create", "x", files=[bad])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("outside the repo working tree", errors[0])
        self.assertIn(bad, errors[0])

    def test_state_dir_path_rejected(self):
        # ~/.claude/projects/<key>/ used to be allowlisted; the design
        # was reversed to keep the runner inside the worktree. Such
        # tickets must be hand-fixed.
        bad = str(Path.home() / ".claude" / "projects" / "claude-apiary" / "state.json")
        steps = [_step(1, "create", "x", files=[bad])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("outside the repo working tree", errors[0])

    def test_relative_traversal_rejected(self):
        # `../../etc/passwd` resolves out of the repo at validation time.
        steps = [_step(1, "create", "x", files=["../../etc/passwd"])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("outside the repo working tree", errors[0])

    def test_windows_style_absolute_path_rejected(self):
        # T5b regression: planner emitted C:\Users\... paths in files[].
        # On POSIX the literal is parsed as a relative path that, when
        # resolved against repo root, falls under the repo (so it would
        # be accepted, technically as relative). Only assert the rejection
        # on platforms where the parser treats it as absolute.
        steps = [_step(1, "create", "x", files=["C:\\Users\\user\\.claude\\CLAUDE.md"])]
        errors = _check_path_allowlist(steps)
        if Path("C:\\Users\\user\\.claude\\CLAUDE.md").is_absolute():
            self.assertEqual(len(errors), 1)
            self.assertIn("outside the repo working tree", errors[0])

    def test_mixed_files_in_one_step(self):
        ok_in_repo = str(validate_plan._REPO_ROOT / "runner" / "foo.py")
        bad = str(self.outside_root / "path.json")
        steps = [_step(1, "create", "x", files=[ok_in_repo, bad])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn(bad, errors[0])

    def test_relative_path_resolved_against_repo_root_not_cwd(self):
        # #232: validate_plan must not depend on cwd. A relative in-repo
        # path like 'runner/foo.py' must be accepted even when invoked
        # from a directory that isn't the repo root.
        import os

        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                steps = [_step(1, "create", "x", files=["runner/validate_plan.py"])]
                self.assertEqual(_check_path_allowlist(steps), [])
                # And the out-of-repo check still fires from odd cwds.
                steps_bad = [_step(1, "create", "x", files=["../../etc/passwd"])]
                errors = _check_path_allowlist(steps_bad)
                self.assertEqual(len(errors), 1)
                self.assertIn("outside the repo working tree", errors[0])
            finally:
                # Must restore cwd BEFORE the TemporaryDirectory __exit__
                # runs on Windows — otherwise rmtree fails because cwd
                # is still inside the tempdir.
                os.chdir(orig_cwd)

    def test_modify_file_existence_resolved_against_repo_root(self):
        # #232: the modify/delete file-existence check must also resolve
        # relative paths against REPO_ROOT, not cwd, so validate_plan can
        # be invoked from anywhere.
        import os

        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                plan = _base_plan(
                    [
                        _step(1, "modify", "noop", files=["runner/validate_plan.py"]),
                    ]
                )
                errors = validate(plan)
                self.assertFalse(
                    any("file not found" in e for e in errors),
                    f"unexpected file-not-found error from non-repo cwd: {errors}",
                )
            finally:
                os.chdir(orig_cwd)

    def test_case_mismatched_absolute_path_accepted_on_windows(self):
        # #234: on Windows the filesystem is case-insensitive, so a
        # planner-emitted path with a different drive-letter case must
        # not be rejected by the allowlist. POSIX filesystems are
        # case-sensitive, so this test only asserts on Windows.
        if os.name != "nt":
            self.skipTest("Windows-only: POSIX filesystems are case-sensitive")
        repo_str = str(validate_plan._REPO_ROOT.resolve())
        # Invert the case of every alpha character to guarantee a mismatch
        # that exercises the normcase path.
        flipped = repo_str.swapcase() + "\\runner\\validate_plan.py"
        steps = [_step(1, "modify", "x", files=[flipped])]
        self.assertEqual(
            _check_path_allowlist(steps), [], f"case-flipped path was rejected: {flipped}"
        )
        # And the gitignored check must still strip the prefix correctly
        # and NOT flag a tracked file.
        self.assertEqual(_check_gitignored_paths(steps), [])

    def test_full_validate_surfaces_allowlist_errors(self):
        bad = str(self.outside_root / "path.json")
        plan = _base_plan(
            [
                _step(1, "create", "noop spec", files=[bad]),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("outside the repo working tree" in e for e in errors),
            f"expected allowlist error in {errors}",
        )


class TestCriteriaCoverageBigrams(unittest.TestCase):
    """#240: the old per-word coverage heuristic false-positived so hard
    that it was effectively a no-op check — a criterion of 'user can log
    in' was 'covered' by any step that said 'user'. Bigram overlap
    demands that at least one adjacent word pair from the criterion
    appears verbatim in step text."""

    def _spec(self, *criteria):
        return {"acceptance_criteria": list(criteria)}

    def test_exact_phrase_match_passes(self):
        spec = self._spec("user can log in")
        # Step mentions the full phrase → bigrams 'user can', 'can log',
        # 'log in' all present → pass.
        steps = [_step(1, "create", "Handle the user can log in flow")]
        self.assertEqual(_check_criteria_coverage(spec, steps), [])

    def test_single_word_overlap_no_longer_passes(self):
        # #240 regression: a step that only mentions 'user' must not
        # satisfy 'user can log in' under the bigram rule.
        spec = self._spec("user can log in")
        steps = [_step(1, "create", "update the user model field")]
        errors = _check_criteria_coverage(spec, steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("not covered", errors[0])

    def test_partial_bigram_match_passes(self):
        # A step that matches one bigram ('log in') but not the full
        # phrase is enough — requiring every bigram would be too strict.
        spec = self._spec("user can log in")
        steps = [_step(1, "create", "Wire up the log in handler")]
        self.assertEqual(_check_criteria_coverage(spec, steps), [])

    def test_criterion_with_only_stopwords_skipped(self):
        # A criterion whose every bigram is stopword-only ('is the',
        # 'the a') produces no usable bigrams, so the check skips it
        # rather than always-erroring.
        spec = self._spec("is the a")
        steps = [_step(1, "create", "noop")]
        self.assertEqual(_check_criteria_coverage(spec, steps), [])

    def test_punctuation_in_step_text_does_not_block_match(self):
        # Step text with punctuation ('subprocess.run(...)') normalizes
        # to whitespace-separated tokens so the bigram still matches.
        spec = self._spec("runs the subprocess run command")
        steps = [_step(1, "create", "code: subprocess.run(['git','status'])")]
        errors = _check_criteria_coverage(spec, steps)
        # 'subprocess run' bigram appears after normalization
        self.assertEqual(errors, [])

    def test_case_insensitive_match(self):
        spec = self._spec("User Can Log In")
        steps = [_step(1, "create", "handle the User Can flow")]
        self.assertEqual(_check_criteria_coverage(spec, steps), [])

    def test_multiple_criteria_one_uncovered(self):
        spec = self._spec("user can log in", "admin can reset password")
        steps = [_step(1, "create", "Wire up user can log in handler")]
        errors = _check_criteria_coverage(spec, steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("admin", errors[0])

    def test_empty_criteria_list_passes(self):
        self.assertEqual(_check_criteria_coverage({}, []), [])
        self.assertEqual(_check_criteria_coverage({"acceptance_criteria": []}, []), [])

    def test_criterion_bigrams_helper(self):
        # Whitebox: verify the bigram helper excludes stopword-only pairs.
        bigrams = _criterion_bigrams("the user can log in to the app")
        # 'the user' — stopword + content → kept
        self.assertIn("the user", bigrams)
        # 'in to' — both stopwords → dropped
        self.assertNotIn("in to", bigrams)
        # 'to the' — both stopwords → dropped
        self.assertNotIn("to the", bigrams)
        # 'the app' — stopword + content → kept
        self.assertIn("the app", bigrams)


class TestFileOverlap(unittest.TestCase):
    """#241: two steps targeting the same file without a depends_on chain
    race via nondeterministic topological sort, so the validator must
    require explicit ordering between them."""

    def test_disjoint_files_pass(self):
        steps = [
            _step(1, "create", "x", files=["runner/a.py"]),
            _step(2, "create", "x", files=["runner/b.py"]),
        ]
        self.assertEqual(_check_file_overlap(steps), [])

    def test_overlapping_files_without_depends_on_rejected(self):
        steps = [
            _step(1, "modify", "x", files=["runner/shared.py"]),
            _step(2, "modify", "x", files=["runner/shared.py"]),
        ]
        errors = _check_file_overlap(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("runner/shared.py", errors[0])
        self.assertIn("depends_on", errors[0])

    def test_overlap_with_direct_depends_on_accepted(self):
        steps = [
            _step(1, "modify", "x", files=["runner/shared.py"]),
            _step(2, "modify", "x", files=["runner/shared.py"], depends_on=[1]),
        ]
        self.assertEqual(_check_file_overlap(steps), [])

    def test_overlap_with_transitive_depends_on_accepted(self):
        # 1 → 2 → 3, step 3 also touches the file step 1 touched.
        steps = [
            _step(1, "modify", "x", files=["runner/shared.py"]),
            _step(2, "modify", "x", files=["other.py"], depends_on=[1]),
            _step(3, "modify", "x", files=["runner/shared.py"], depends_on=[2]),
        ]
        self.assertEqual(_check_file_overlap(steps), [])

    def test_overlap_with_reverse_depends_on_accepted(self):
        # step 1 depends on step 2 — unusual order but still fully
        # ordered, so no race.
        steps = [
            _step(1, "modify", "x", files=["runner/shared.py"], depends_on=[2]),
            _step(2, "modify", "x", files=["runner/shared.py"]),
        ]
        self.assertEqual(_check_file_overlap(steps), [])

    def test_path_slash_normalization(self):
        # The same file spelled with \ and / should count as overlap.
        steps = [
            _step(1, "modify", "x", files=["runner/shared.py"]),
            _step(2, "modify", "x", files=["runner\\shared.py"]),
        ]
        errors = _check_file_overlap(steps)
        self.assertEqual(len(errors), 1)

    def test_three_way_overlap_all_pairs_checked(self):
        # Steps 1, 2, 3 all touch the same file with no ordering → two
        # unordered pairs are unordered (1/2 and 1/3 — step 2 depends
        # on step 1, so that pair is OK; step 3 has no deps).
        steps = [
            _step(1, "modify", "x", files=["shared.py"]),
            _step(2, "modify", "x", files=["shared.py"], depends_on=[1]),
            _step(3, "modify", "x", files=["shared.py"]),
        ]
        errors = _check_file_overlap(steps)
        # Expected: 1–3 and 2–3 are unordered → 2 errors.
        self.assertEqual(len(errors), 2)
        all_msg = " ".join(errors)
        self.assertIn("steps 1 and 3", all_msg)
        self.assertIn("steps 2 and 3", all_msg)

    def test_multiple_shared_files_between_same_pair(self):
        # Same pair of steps shares multiple files → one error per file.
        steps = [
            _step(1, "modify", "x", files=["a.py", "b.py"]),
            _step(2, "modify", "x", files=["a.py", "b.py"]),
        ]
        errors = _check_file_overlap(steps)
        self.assertEqual(len(errors), 2)

    def test_full_validate_surfaces_overlap_errors(self):
        plan = _base_plan(
            [
                _step(1, "create", "spec a", files=["runner/shared.py"]),
                _step(2, "modify", "spec b", files=["runner/shared.py"]),
            ]
        )
        errors = validate(plan)
        self.assertTrue(any("race" in e for e in errors), f"expected overlap error in {errors}")


class TestGitignoredPaths(unittest.TestCase):
    """#233: validate_plan must reject gitignored paths up front, otherwise
    commit_files raises a generic 'no changes' error at runtime when
    `git add` silently no-ops on the ignored path.

    These tests exercise the live repo's .gitignore (runner/specs/ is
    ignored, runner/validate_plan.py is tracked), not a fixture repo —
    keeps the test simple and catches accidental un-ignoring."""

    def test_tracked_path_accepted(self):
        steps = [_step(1, "modify", "x", files=["runner/validate_plan.py"])]
        self.assertEqual(_check_gitignored_paths(steps), [])

    def test_gitignored_path_rejected(self):
        # runner/specs/*.json matches a gitignore pattern in this repo.
        steps = [_step(1, "create", "x", files=["runner/specs/fake-uuid.json"])]
        errors = _check_gitignored_paths(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("gitignored", errors[0])
        self.assertIn("runner/specs/fake-uuid.json", errors[0])

    def test_out_of_repo_path_skipped_by_gitignore_check(self):
        # Out-of-repo paths are handled by the allowlist, not here. The
        # gitignore check must silently skip them (not crash, not error).
        with tempfile.TemporaryDirectory() as tmp:
            bad = str(Path(tmp).resolve() / "random.json")
            steps = [_step(1, "create", "x", files=[bad])]
            self.assertEqual(_check_gitignored_paths(steps), [])

    def test_mixed_tracked_and_ignored_in_one_step(self):
        steps = [
            _step(
                1,
                "create",
                "x",
                files=[
                    "runner/validate_plan.py",
                    "runner/specs/another.json",
                ],
            )
        ]
        errors = _check_gitignored_paths(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("runner/specs/another.json", errors[0])

    def test_full_validate_surfaces_gitignore_errors(self):
        plan = _base_plan(
            [
                _step(1, "create", "noop spec", files=["runner/specs/x.json"]),
            ]
        )
        errors = validate(plan)
        self.assertTrue(
            any("gitignored" in e for e in errors), f"expected gitignore error in {errors}"
        )

    def test_git_unavailable_skips_silently(self):
        # If git is not on PATH (or any OSError), the check is a no-op
        # rather than a hard failure. Simulated by patching subprocess.run
        # to raise FileNotFoundError.
        steps = [_step(1, "create", "x", files=["runner/specs/x.json"])]
        with mock.patch(
            "runner.validate_plan.subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            self.assertEqual(_check_gitignored_paths(steps), [])


class TestPostConditionsSchema(unittest.TestCase):
    """#T-2026-122 phase 2: post_conditions is optional but when present
    must be well-formed. Invalid structure surfaces at plan validation
    time rather than failing mid-run."""

    def _step_with_pcs(self, pcs):
        s = _step(1, "create", "code", files=["new_file.py"])
        s["post_conditions"] = pcs
        return s

    def test_no_post_conditions_is_valid(self):
        plan = _base_plan([_step(1, "create", "x", files=["runner/foo.py"])])
        # validate() requires modify targets to exist — a create step
        # with a new path is fine.
        errors = validate(plan)
        self.assertEqual([e for e in errors if "post_conditions" in e], [])

    def test_post_conditions_must_be_list(self):
        plan = _base_plan([self._step_with_pcs("not a list")])
        errors = validate(plan)
        self.assertTrue(
            any("'post_conditions' must be an array" in e for e in errors),
            errors,
        )

    def test_unknown_type_rejected(self):
        plan = _base_plan([self._step_with_pcs([{"type": "garbage", "file": "new_file.py"}])])
        errors = validate(plan)
        self.assertTrue(
            any("invalid type 'garbage'" in e for e in errors),
            errors,
        )

    def test_file_contains_requires_text(self):
        plan = _base_plan([self._step_with_pcs([{"type": "file_contains", "file": "new_file.py"}])])
        errors = validate(plan)
        self.assertTrue(
            any("'text' is required" in e for e in errors),
            errors,
        )

    def test_well_formed_conditions_pass(self):
        plan = _base_plan(
            [
                self._step_with_pcs(
                    [
                        {"type": "file_contains", "file": "new_file.py", "text": "def x"},
                        {"type": "file_exists", "file": "new_file.py"},
                        {"type": "file_absent", "file": "old_file.py"},
                        {"type": "file_lacks", "file": "new_file.py", "text": "banned"},
                    ]
                )
            ]
        )
        errors = [e for e in validate(plan) if "post_conditions" in e]
        self.assertEqual(errors, [])

    def test_outside_repo_path_rejected(self):
        plan = _base_plan([self._step_with_pcs([{"type": "file_exists", "file": "/etc/passwd"}])])
        errors = validate(plan)
        self.assertTrue(
            any("outside the repo" in e for e in errors),
            errors,
        )


class TestTargetRepoRoot(unittest.TestCase):
    """review runner Bug 2: paths were resolved against apiary's checkout, so
    a `modify` step on a file that exists only in --target-repo X failed with
    'file not found' on every attempt and multi-repo runs never reached
    stage 4."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name).resolve() / "target"
        (self.target / "src").mkdir(parents=True)
        (self.target / "src" / "only_here.py").write_text("x = 1\n", encoding="utf-8")

    def _plan(self, **extra):
        plan = _base_plan(
            [
                _step(1, "modify", "Adjust the value.", files=["src/only_here.py"]),
            ]
        )
        plan.update(extra)
        return plan

    def test_file_only_in_the_target_is_found(self):
        errors = validate(self._plan(target_repo=str(self.target)))
        self.assertEqual(
            [e for e in errors if "file not found" in e],
            [],
            errors,
        )

    def test_without_a_target_the_same_plan_is_rejected(self):
        errors = validate(self._plan())
        self.assertTrue(
            any("file not found" in e for e in errors),
            errors,
        )

    def test_explicit_repo_root_overrides_the_field(self):
        errors = validate(self._plan(), repo_root=self.target)
        self.assertEqual(
            [e for e in errors if "file not found" in e],
            [],
            errors,
        )

    def test_the_root_is_restored_after_the_call(self):
        before = validate_plan._REPO_ROOT
        validate(self._plan(target_repo=str(self.target)))
        self.assertEqual(validate_plan._REPO_ROOT, before)

    def test_a_non_apiary_target_does_not_inherit_apiary_banned_tokens(self):
        """The banned-token comparison must use apiary's own root, not the
        rebound one — otherwise every target looks like apiary."""
        self.assertEqual(_resolve_banned_tokens(str(self.target)), {})
        self.assertNotEqual(_resolve_banned_tokens(None), {})


class TestRemovalCoverageDefinitionFilter(unittest.TestCase):
    """Overnight 2026-08-30/31: prose-level symbol extraction flagged
    identifiers the steps merely mentioned (HookResult, sort_keys, to_dict)
    and demanded every repo file that greps for them, so dead-code plans
    could never validate. The check now only fires when the symbol's
    definition lives inside the plan's own files."""

    def _run(self, steps, references, definitions):
        """Run _check_removal_coverage with git grep stubbed out.

        references/definitions: repo-relative paths returned for the
        reference grep (-w) and the definition grep (-lE) respectively.
        """

        def fake_run(cmd, **kwargs):
            out = definitions if "-lE" in cmd else references
            return subprocess.CompletedProcess(
                cmd,
                0 if out else 1,
                stdout="\n".join(out) + ("\n" if out else ""),
                stderr="",
            )

        with mock.patch.object(validate_plan.subprocess, "run", side_effect=fake_run):
            return validate_plan._check_removal_coverage(steps)

    def _removal_step(self, files):
        return _step(
            1,
            "modify",
            "Apply the edit.",
            description="Remove the unused function stale_helper from the module.",
            files=files,
        )

    def test_symbol_defined_in_plan_file_and_referenced_outside_is_flagged(self):
        errors = self._run(
            [self._removal_step(["pkg/mod.py"])],
            references=["pkg/mod.py", "pkg/other.py"],
            definitions=["pkg/mod.py"],
        )
        self.assertTrue(any("stale_helper" in e for e in errors), errors)

    def test_symbol_with_no_definition_anywhere_is_skipped(self):
        # sort_keys / cache_read case: a kwarg or dict key, not a symbol.
        errors = self._run(
            [self._removal_step(["pkg/mod.py"])],
            references=["pkg/mod.py", "pkg/other.py"],
            definitions=[],
        )
        self.assertEqual(errors, [])

    def test_symbol_defined_outside_the_plan_is_skipped(self):
        # HookResult case: the step mentions a class defined elsewhere.
        errors = self._run(
            [self._removal_step(["pkg/mod.py"])],
            references=["pkg/mod.py", "pkg/other.py"],
            definitions=["core/hook_context.py"],
        )
        self.assertEqual(errors, [])

    def test_symbol_defined_both_in_and_out_of_the_plan_is_skipped(self):
        # to_dict case: many classes define the same method name; outside
        # references bind to their own definitions.
        errors = self._run(
            [self._removal_step(["pkg/mod.py"])],
            references=["pkg/mod.py", "pkg/other.py"],
            definitions=["pkg/mod.py", "gui/transcript.py"],
        )
        self.assertEqual(errors, [])

    def test_fully_covered_removal_stays_clean(self):
        errors = self._run(
            [self._removal_step(["pkg/mod.py", "pkg/other.py"])],
            references=["pkg/mod.py", "pkg/other.py"],
            definitions=["pkg/mod.py"],
        )
        self.assertEqual(errors, [])


class TestChangeMapCoverage(unittest.TestCase):
    """Overnight 2026-08-29: the executor's commit tripped the repo's
    change-map doc gate at stage 4, after the tokens were already spent.
    The validator now applies the same map at plan time."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name).resolve() / "target"
        (self.target / "docs").mkdir(parents=True)
        (self.target / "src").mkdir()
        (self.target / "src" / "mapped.py").write_text("x = 1\n", encoding="utf-8")
        (self.target / "docs" / "thing.md").write_text("doc\n", encoding="utf-8")
        (self.target / "docs" / "change_map.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "id": "thing",
                            "code": ["src/mapped.py"],
                            "docs": ["docs/thing.md"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _check(self, steps):
        with validate_plan.repo_root_scope(self.target):
            return validate_plan._check_change_map_coverage(steps)

    def test_mapped_code_without_doc_is_rejected(self):
        errors = self._check([_step(1, "modify", "spec", files=["src/mapped.py"])])
        self.assertTrue(any("change-map gate" in e and "'thing'" in e for e in errors), errors)

    def test_doc_in_the_same_step_passes(self):
        errors = self._check([_step(1, "modify", "spec", files=["src/mapped.py", "docs/thing.md"])])
        self.assertEqual(errors, [])

    def test_doc_in_a_different_step_is_rejected(self):
        """2026-08-31 (d2ac4652): the executor commits per step, so a doc
        updated in a later step does not unblock the mapped-code commit --
        exactly how the plan-wide union check let the 08-29 plan through."""
        errors = self._check(
            [
                _step(1, "modify", "spec", files=["src/mapped.py"]),
                _step(2, "modify", "spec", files=["docs/thing.md"], depends_on=[1]),
            ]
        )
        self.assertTrue(any("step[0]" in e and "'thing'" in e for e in errors), errors)

    def test_test_and_verify_steps_are_exempt(self):
        errors = self._check(
            [
                _step(1, "test", "python -m unittest x", files=["src/mapped.py"]),
                _step(2, "verify", "check it", files=["src/mapped.py"]),
            ]
        )
        self.assertEqual(errors, [])

    def test_docs_unchanged_attestation_passes(self):
        step = _step(1, "modify", "spec", files=["src/mapped.py"])
        step["docs_unchanged"] = True
        self.assertEqual(self._check([step]), [])

    def test_unmapped_files_pass(self):
        errors = self._check([_step(1, "modify", "spec", files=["src/free.py"])])
        self.assertEqual(errors, [])

    def test_repo_without_a_map_passes(self):
        with validate_plan.repo_root_scope(Path(self._tmp.name)):
            errors = validate_plan._check_change_map_coverage(
                [_step(1, "modify", "spec", files=["src/mapped.py"])]
            )
        self.assertEqual(errors, [])

    def test_docs_unchanged_must_be_boolean(self):
        step = _step(1, "create", "make a file", files=["src/new_file.py"])
        step["docs_unchanged"] = "yes"
        errors = validate(_base_plan([step]), repo_root=self.target)
        self.assertTrue(any("docs_unchanged" in e and "boolean" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
