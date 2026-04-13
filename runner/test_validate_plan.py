#!/usr/bin/env python3
"""Tests for runner/validate_plan.py.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner.validate_plan import (
    validate,
    _check_test_code_spec_format,
    _check_banned_tokens,
    _check_test_failure_language,
    _check_path_allowlist,
    _check_gitignored_paths,
    _check_criteria_coverage,
    _criterion_bigrams,
    _check_file_overlap,
)
from runner import validate_plan


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
        plan = _base_plan([
            _step(1, "create", "Make a file", files=["runner/new_file.py"]),
            _step(2, "test", "Run the tests"),
        ])
        errors = validate(plan)
        self.assertTrue(any("prose word" in e for e in errors),
                        f"expected prose-word error in {errors}")


class TestTestFailureLanguage(unittest.TestCase):
    """The validator must reject test-action steps whose description signals
    expected failure. The executor treats every test step as a hard pass/fail
    gate, so a 'this run is expected to report violations' step always aborts.
    Caught in T5b plan step 3 (#211)."""

    def test_clean_test_step_passes(self):
        steps = [_step(1, "test", "python -m unittest runner.test_foo",
                       description="Verify the new helper passes its unit tests")]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_expected_to_fail_rejected(self):
        steps = [_step(1, "test", "python audit.py",
                       description="Run the audit (expected to fail before fix)")]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected to fail", errors[0])

    def test_expected_to_report_violations_rejected(self):
        steps = [_step(1, "test", "python audit.py",
                       description="Run audit (this run is expected to report violations; "
                                   "it gates subsequent fix steps)")]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        # Both phrases match — the function returns on first match.
        self.assertTrue(
            "expected to report violations" in errors[0]
            or "this run is expected to" in errors[0]
        )

    def test_should_fail_rejected(self):
        steps = [_step(1, "test", "python check.py",
                       description="The pre-fix snapshot — should fail until step 4 lands")]
        self.assertEqual(len(_check_test_failure_language(steps)), 1)

    def test_non_test_action_ignored(self):
        steps = [_step(1, "create", "make file",
                       description="this run is expected to fail before the fix")]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_full_validate_surfaces_failure_language(self):
        plan = _base_plan([
            _step(1, "create", "Add helper", files=["runner/new_file.py"]),
            _step(2, "test", "python audit.py",
                  description="Run the audit script (expected to report violations; "
                              "gates subsequent fix steps)"),
        ])
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
        steps = [_step(1, "create", "freeform pseudocode here",
                       description="Add pytest tests for the new module")]
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
        plan = _base_plan([
            _step(1, "create", "Write a pytest test file",
                  files=["runner/test_new.py"]),
        ])
        errors = validate(plan)
        self.assertTrue(any("banned token" in e for e in errors),
                        f"expected banned-token error in {errors}")


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
        self.outside_root = Path(self._tmp.name) / "outside"
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
        bad = str(Path.home() / ".claude" / "projects" / "claude-apiary" / "backfill_skip.json")
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
        steps = [_step(1, "create", "x", files=["C:\\Users\\amedi\\.claude\\CLAUDE.md"])]
        errors = _check_path_allowlist(steps)
        if Path("C:\\Users\\amedi\\.claude\\CLAUDE.md").is_absolute():
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
                plan = _base_plan([
                    _step(1, "modify", "noop", files=["runner/validate_plan.py"]),
                ])
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
        self.assertEqual(_check_path_allowlist(steps), [],
                         f"case-flipped path was rejected: {flipped}")
        # And the gitignored check must still strip the prefix correctly
        # and NOT flag a tracked file.
        self.assertEqual(_check_gitignored_paths(steps), [])

    def test_full_validate_surfaces_allowlist_errors(self):
        bad = str(self.outside_root / "path.json")
        plan = _base_plan([
            _step(1, "create", "noop spec", files=[bad]),
        ])
        errors = validate(plan)
        self.assertTrue(any("outside the repo working tree" in e for e in errors),
                        f"expected allowlist error in {errors}")


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
        self.assertEqual(
            _check_criteria_coverage({"acceptance_criteria": []}, []), []
        )

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
        plan = _base_plan([
            _step(1, "create", "spec a", files=["runner/shared.py"]),
            _step(2, "modify", "spec b", files=["runner/shared.py"]),
        ])
        errors = validate(plan)
        self.assertTrue(any("race" in e for e in errors),
                        f"expected overlap error in {errors}")


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
            bad = str(Path(tmp) / "random.json")
            steps = [_step(1, "create", "x", files=[bad])]
            self.assertEqual(_check_gitignored_paths(steps), [])

    def test_mixed_tracked_and_ignored_in_one_step(self):
        steps = [_step(1, "create", "x", files=[
            "runner/validate_plan.py",
            "runner/specs/another.json",
        ])]
        errors = _check_gitignored_paths(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("runner/specs/another.json", errors[0])

    def test_full_validate_surfaces_gitignore_errors(self):
        plan = _base_plan([
            _step(1, "create", "noop spec", files=["runner/specs/x.json"]),
        ])
        errors = validate(plan)
        self.assertTrue(any("gitignored" in e for e in errors),
                        f"expected gitignore error in {errors}")

    def test_git_unavailable_skips_silently(self):
        # If git is not on PATH (or any OSError), the check is a no-op
        # rather than a hard failure. Simulated by patching subprocess.run
        # to raise FileNotFoundError.
        steps = [_step(1, "create", "x", files=["runner/specs/x.json"])]
        with mock.patch("runner.validate_plan.subprocess.run",
                        side_effect=FileNotFoundError("no git")):
            self.assertEqual(_check_gitignored_paths(steps), [])


if __name__ == "__main__":
    unittest.main()
