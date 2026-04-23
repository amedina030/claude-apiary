#!/usr/bin/env python3
"""Tests for core.apiary_bootstrap — CLI, state, drift prompt, error paths.

Covers acceptance criteria AC-1 through AC-9 and AC-21.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import apiary_bootstrap


def _make_apiary_repo(tmp: Path) -> Path:
    apiary = tmp / "apiary"
    (apiary / "profiles").mkdir(parents=True)
    return apiary


def _write_profile(apiary: Path, name: str, body: str) -> Path:
    path = apiary / "profiles" / f"{name}.jsonc"
    path.write_text(body, encoding="utf-8")
    return path


def _run(args: list[str], stdin_tty: bool = False, stdin_answer: str = "") -> int:
    """Invoke main(argv) with stdin patched as TTY-or-not."""
    fake_stdin = io.StringIO(stdin_answer)
    fake_stdin.isatty = lambda: stdin_tty  # type: ignore
    with mock.patch("sys.stdin", fake_stdin):
        return apiary_bootstrap.main(args)


class _BootstrapHarness(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = _make_apiary_repo(self.tmp)
        self.target = self.tmp / "target"
        self.target.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self) -> dict:
        path = self.target / ".claude" / "settings.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _state(self) -> dict:
        path = self.target / ".apiary" / "bootstrap_state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _cli(self, *extra: str, stdin_tty: bool = False, stdin_answer: str = "") -> int:
        args = [
            "--profile",
            extra[0],
            "--target",
            str(self.target),
            "--apiary-repo",
            str(self.apiary),
        ] + list(extra[1:])
        return _run(args, stdin_tty=stdin_tty, stdin_answer=stdin_answer)


class TestFreshBootstrap(_BootstrapHarness):

    def test_ac1_fresh_target_writes_settings_and_state(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["X"]}}',
        )
        rc = self._cli("base")
        self.assertEqual(rc, 0)
        settings = self._settings()
        self.assertEqual(settings, {"permissions": {"allow": ["X"]}})

        state = self._state()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["profile"], "base")
        self.assertEqual(state["profiles_applied"], ["base"])
        self.assertIn("base", state["profile_content_hashes"])
        self.assertEqual(state["applied_apiary_keys"], ["permissions"])
        self.assertIn("last_bootstrap_ts", state)

    def test_ac1_extends_chain_applied(self):
        _write_profile(self.apiary, "base", '{"$schema_version": 1, "permissions": {"allow": ["A"]}}')
        _write_profile(
            self.apiary,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"allow": ["B"]}}',
        )
        rc = self._cli("child")
        self.assertEqual(rc, 0)
        self.assertEqual(self._settings()["permissions"]["allow"], ["A", "B"])
        state = self._state()
        self.assertEqual(state["profiles_applied"], ["base", "child"])


class TestNonApiaryKeysPreserved(_BootstrapHarness):

    def test_ac2_top_level_non_apiary_keys_untouched(self):
        claude_dir = self.target / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "user_field": {"nested": "keep me"},
                    "permissions": {"allow": ["USER_X"]},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY_Y"]}}',
        )
        # --force because existing permissions.allow has user content that
        # the profile replaces — first-run safety warning would otherwise prompt.
        rc = self._cli("base", "--force")
        self.assertEqual(rc, 0)
        settings = self._settings()
        # Top-level non-apiary keys preserved byte-for-byte.
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["user_field"], {"nested": "keep me"})
        # Apiary-owned top-level key ("permissions") fully replaced — users
        # should layer their entries via .claude/settings.local.json.
        self.assertEqual(settings["permissions"], {"allow": ["APIARY_Y"]})


class TestReRunDrift(_BootstrapHarness):

    def setUp(self):
        super().setUp()
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V1"]}}',
        )
        rc = self._cli("base")
        self.assertEqual(rc, 0)

    def test_ac3_rerun_with_changed_profile_prompts_and_applies_on_yes(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V2"]}}',
        )
        rc = self._cli("base", stdin_tty=True, stdin_answer="y\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self._settings()["permissions"]["allow"], ["V2"])

    def test_ac3_rerun_with_no_skips_apply(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V2"]}}',
        )
        rc = self._cli("base", stdin_tty=True, stdin_answer="n\n")
        self.assertEqual(rc, 1)
        self.assertEqual(self._settings()["permissions"]["allow"], ["V1"])

    def test_ac3_force_skips_prompt(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V2"]}}',
        )
        rc = self._cli("base", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(self._settings()["permissions"]["allow"], ["V2"])

    def test_rerun_without_changes_no_prompt_needed(self):
        # Profile unchanged — settings match — re-run succeeds without prompt.
        rc = self._cli("base")
        self.assertEqual(rc, 0)


class TestReplaceDeepMerge(_BootstrapHarness):

    def test_replace_wrapper_replaces_list(self):
        _write_profile(self.apiary, "base", '{"$schema_version": 1, "permissions": {"allow": ["A"]}}')
        _write_profile(
            self.apiary,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"allow": {"$replace": ["ONLY"]}}}',
        )
        rc = self._cli("child")
        self.assertEqual(rc, 0)
        self.assertEqual(self._settings()["permissions"]["allow"], ["ONLY"])


class TestErrors(_BootstrapHarness):

    def test_ac5_cycle_exits_non_zero(self):
        _write_profile(self.apiary, "a", '{"$schema_version": 1, "extends": ["b"]}')
        _write_profile(self.apiary, "b", '{"$schema_version": 1, "extends": ["a"]}')
        rc = self._cli("a")
        self.assertEqual(rc, 2)

    def test_ac6_unknown_schema_version_exits_non_zero(self):
        _write_profile(self.apiary, "x", '{"$schema_version": 999}')
        rc = self._cli("x")
        self.assertEqual(rc, 2)

    def test_ac7_missing_profile_lists_available(self):
        _write_profile(self.apiary, "exists", '{"$schema_version": 1}')
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("ghost")
        self.assertEqual(rc, 2)
        self.assertIn("exists", stderr.getvalue())

    def test_ac8_jsonc_parse_error_reports_file_and_line(self):
        _write_profile(self.apiary, "broken", '{"$schema_version": 1,\n  "x": ,\n}')
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("broken")
        self.assertEqual(rc, 2)
        err_text = stderr.getvalue()
        self.assertIn("broken.jsonc", err_text)


class TestFirstRunSafetyWarning(_BootstrapHarness):

    def _seed_existing_settings(self, payload: dict) -> None:
        claude_dir = self.target / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def test_first_run_empty_settings_no_warning(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base")
        self.assertEqual(rc, 0)
        self.assertNotIn("warning:", stderr.getvalue())

    def test_first_run_non_apiary_keys_no_warning(self):
        self._seed_existing_settings({"theme": "dark", "custom": "keep"})
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base")
        self.assertEqual(rc, 0)
        self.assertNotIn("warning:", stderr.getvalue())

    def test_first_run_wipe_candidate_in_permissions_deny_prompts(self):
        self._seed_existing_settings(
            {"permissions": {"allow": ["USER_X"], "deny": ["Read(secrets/*)"]}}
        )
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base", stdin_tty=True, stdin_answer="y\n")
        self.assertEqual(rc, 0)
        err = stderr.getvalue()
        self.assertIn("warning:", err.lower())
        self.assertIn("Read(secrets/*)", err)
        self.assertIn("USER_X", err)
        self.assertIn("settings.local.json", err)

    def test_first_run_wipe_declined_exits_one(self):
        self._seed_existing_settings(
            {"permissions": {"deny": ["Read(secrets/*)"]}}
        )
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base", stdin_tty=True, stdin_answer="n\n")
        self.assertEqual(rc, 1)
        # Settings not written.
        self.assertEqual(
            self._settings(),
            {"permissions": {"deny": ["Read(secrets/*)"]}},
        )

    def test_first_run_force_skips_prompt_but_still_warns(self):
        self._seed_existing_settings(
            {"permissions": {"deny": ["Read(secrets/*)"]}}
        )
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base", "--force")
        self.assertEqual(rc, 0)
        self.assertIn("warning:", stderr.getvalue().lower())
        # Settings written — apiary's permissions now in place.
        self.assertEqual(self._settings()["permissions"], {"allow": ["APIARY"]})

    def test_first_run_non_tty_without_force_errors(self):
        self._seed_existing_settings(
            {"permissions": {"deny": ["Read(secrets/*)"]}}
        )
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["APIARY"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base", stdin_tty=False)
        self.assertEqual(rc, 1)
        self.assertIn("--force", stderr.getvalue())


class TestPreExistingApiaryDir(_BootstrapHarness):

    def test_ac21_existing_apiary_without_state_is_fresh(self):
        # Pre-seed .apiary/scribe/ to simulate a scribe-only repo.
        scribe_dir = self.target / ".apiary" / "scribe"
        scribe_dir.mkdir(parents=True)
        (scribe_dir / "notes.jsonl").write_text('{"id": "T-1"}\n', encoding="utf-8")

        _write_profile(self.apiary, "base", '{"$schema_version": 1, "permissions": {"allow": ["X"]}}')
        rc = self._cli("base")
        self.assertEqual(rc, 0)
        state = self._state()
        self.assertEqual(state["profile"], "base")
        # Existing scribe file untouched.
        self.assertTrue((scribe_dir / "notes.jsonl").is_file())
        self.assertEqual(
            (scribe_dir / "notes.jsonl").read_text(encoding="utf-8"),
            '{"id": "T-1"}\n',
        )


class TestNonTTYReRun(_BootstrapHarness):

    def test_rerun_without_tty_and_without_force_errors(self):
        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V1"]}}',
        )
        rc = self._cli("base")
        self.assertEqual(rc, 0)

        _write_profile(
            self.apiary,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["V2"]}}',
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self._cli("base", stdin_tty=False)
        self.assertEqual(rc, 1)
        self.assertIn("--force", stderr.getvalue())


class TestSummaryOutput(_BootstrapHarness):

    def test_summary_lists_applied_profiles_and_keys(self):
        _write_profile(self.apiary, "base", '{"$schema_version": 1, "permissions": {"allow": ["A"]}}')
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self._cli("base")
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("base", out)
        self.assertIn("permissions", out)
        self.assertIn(".claude/settings.json", out.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
