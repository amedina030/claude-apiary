#!/usr/bin/env python3
"""Tests for scripts/install_context_rules.py.

Each test runs the installer's `main()` against a temporary CLAUDE.md and
asserts the resulting file content + exit code. Source rules use the real
context-rules/ directory in the repo (the Layer 4 conformance test in
core/test_context_rules.py guarantees those files parse).
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr  # noqa: E402
from scripts import install_context_rules as ir  # noqa: E402


class _Run:
    """Helper that runs the CLI in-process and captures stdout/stderr."""

    def __init__(self, exit_code: int, stdout: str, stderr: str):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def run(*argv: str) -> _Run:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ir.main(list(argv))
    return _Run(rc, out.getvalue(), err.getvalue())


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "CLAUDE.md"

    def _write(self, text: str) -> None:
        self.target.write_text(text, encoding="utf-8")

    def _read(self) -> str:
        return self.target.read_text(encoding="utf-8")

    def _common(self, *extra: str) -> list[str]:
        return ["--target", str(self.target), *extra]


class TestInstallAll(InstallerTestCase):
    def test_fresh_install(self):
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, 0)
        text = self._read()
        self.assertIn(cr.OUTER_START, text)
        self.assertIn(cr.OUTER_END, text)
        zone = cr.find_managed_zone(text)
        ids = {ir_.id for ir_ in zone.rules}
        self.assertIn("recover_from_trivial_errors", ids)
        self.assertIn("keep_chaining_mid_plan", ids)
        self.assertIn("no_coauthored_by", ids)

    def test_idempotent(self):
        run(*self._common("--install-all"))
        before = self._read()
        run(*self._common("--install-all"))
        self.assertEqual(self._read(), before)

    def test_preserves_user_content(self):
        self._write("# my notes\n\nhello\n")
        run(*self._common("--install-all"))
        text = self._read()
        self.assertIn("# my notes", text)
        self.assertIn("hello", text)
        self.assertIn(cr.OUTER_START, text)

    def test_dry_run_does_not_write(self):
        self._write("# pre\n")
        result = run(*self._common("--install-all", "--dry-run"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self._read(), "# pre\n")
        self.assertIn("would install", result.stdout)


class TestUninstall(InstallerTestCase):
    def test_uninstall_one(self):
        run(*self._common("--install-all"))
        run(*self._common("--uninstall", "no_coauthored_by"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertNotIn("no_coauthored_by", ids)
        self.assertIn("recover_from_trivial_errors", ids)

    def test_uninstall_last_removes_zone(self):
        run(*self._common("--install", "no_coauthored_by"))
        run(*self._common("--uninstall", "no_coauthored_by"))
        text = self._read()
        self.assertNotIn(cr.OUTER_START, text)


class TestSync(InstallerTestCase):
    def test_sync_after_source_change(self):
        # Install a custom rules dir, then mutate one rule's source body.
        rules_dir = Path(self._tmp.name) / "rules" / "behavioral"
        rules_dir.mkdir(parents=True)
        rule_path = rules_dir / "demo.md"
        rule_path.write_text(
            "---\nid: demo\ntitle: Demo\ncategory: behavioral\nrequires: []\n---\nOld body.\n",
            encoding="utf-8",
        )
        common = ["--target", str(self.target), "--rules-dir", str(rules_dir.parent)]

        run(*common, "--install-all")
        old_text = self._read()

        rule_path.write_text(
            "---\nid: demo\ntitle: Demo\ncategory: behavioral\nrequires: []\n---\nNew body.\n",
            encoding="utf-8",
        )
        # --check should now report drift
        check = run(*common, "--check")
        self.assertEqual(check.exit_code, ir.EXIT_DRIFT)

        # --sync should re-render
        run(*common, "--sync")
        new_text = self._read()
        self.assertNotEqual(new_text, old_text)
        self.assertIn("New body.", new_text)

        # --check clean again
        check2 = run(*common, "--check")
        self.assertEqual(check2.exit_code, 0)


class TestCheck(InstallerTestCase):
    def test_clean_install_check(self):
        run(*self._common("--install-all"))
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, 0)
        self.assertIn("clean", result.stdout)

    def test_no_zone_is_clean(self):
        self._write("# nothing here\n")
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, 0)


class TestTamperEnforcement(InstallerTestCase):
    def test_body_tamper_blocks_install(self):
        run(*self._common("--install-all"))
        text = self._read()
        # Replace recover_from_trivial_errors body with a hand edit (still
        # inside the zone, between its sentinels).
        tampered = text.replace("fix it and retry", "TAMPERED HERE")
        self._write(tampered)
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)
        self.assertIn("hand-edited", result.stderr)

    def test_force_overrides_tamper(self):
        run(*self._common("--install-all"))
        text = self._read()
        tampered = text.replace("fix it and retry", "TAMPERED HERE")
        self._write(tampered)
        result = run(*self._common("--install-all", "--force"))
        self.assertEqual(result.exit_code, 0)
        # After force install, the source body should be back.
        self.assertIn("fix it and retry", self._read())
        self.assertNotIn("TAMPERED HERE", self._read())

    def test_zone_tamper_blocks(self):
        run(*self._common("--install-all"))
        # Delete the end marker to break the zone.
        broken = self._read().replace(cr.OUTER_END, "")
        self._write(broken)
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)

    def test_check_reports_tamper_exit_code(self):
        run(*self._common("--install-all"))
        text = self._read()
        tampered = text.replace("fix it and retry", "TAMPERED HERE")
        self._write(tampered)
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)
        self.assertIn("TAMPERED", result.stdout)


class TestReplaceStopgap(InstallerTestCase):
    def test_strips_known_stopgap_paragraphs(self):
        # Simulate a CLAUDE.md with the inline stopgap content above the zone.
        stopgap = (
            "# Global Claude Code Rules\n\n"
            "## Behavioral rules\n\n"
            "### Recover from trivial errors inline\n\n"
            "Some inline content.\n\n"
            "### Keep chaining after successful mid-plan steps\n\n"
            "More inline content.\n\n"
            "## Other section\n\n"
            "Keep this.\n"
        )
        self._write(stopgap)
        result = run(*self._common("--install-all", "--replace-stopgap"))
        self.assertEqual(result.exit_code, 0)
        text = self._read()
        self.assertNotIn("### Recover from trivial errors inline", text.split(cr.OUTER_START)[0])
        self.assertNotIn("### Keep chaining after successful mid-plan steps", text.split(cr.OUTER_START)[0])
        self.assertIn("Other section", text)
        self.assertIn("Keep this.", text)
        self.assertIn(cr.OUTER_START, text)


class TestList(InstallerTestCase):
    def test_list_runs_clean(self):
        result = run(*self._common("--list"))
        self.assertEqual(result.exit_code, 0)
        self.assertIn("recover_from_trivial_errors", result.stdout)
        self.assertIn("not installed", result.stdout)

    def test_list_after_install(self):
        run(*self._common("--install-all"))
        result = run(*self._common("--list"))
        self.assertIn("installed", result.stdout)


class TestDiff(InstallerTestCase):
    def test_diff_no_change(self):
        run(*self._common("--install-all"))
        result = run(*self._common("--diff", "no_coauthored_by"))
        self.assertEqual(result.exit_code, 0)
        # No diff lines when bodies match
        self.assertEqual(result.stdout, "")

    def test_diff_unknown_rule(self):
        result = run(*self._common("--diff", "no_such_rule"))
        self.assertEqual(result.exit_code, ir.EXIT_USAGE)


class TestInstallSpecific(InstallerTestCase):
    def test_install_one(self):
        run(*self._common("--install", "no_coauthored_by"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertEqual(ids, {"no_coauthored_by"})

    def test_install_unknown(self):
        result = run(*self._common("--install", "no_such_rule"))
        self.assertEqual(result.exit_code, ir.EXIT_USAGE)

    def test_install_category(self):
        run(*self._common("--install-category", "behavioral"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertIn("recover_from_trivial_errors", ids)


if __name__ == "__main__":
    unittest.main()
