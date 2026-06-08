#!/usr/bin/env python3
"""Tests for scripts/install_context_rules.py.

Each test runs the installer's `main()` against a temporary CLAUDE.md and
asserts the resulting file content + exit code. Tests use either the real
context-rules/ directory (which now contains only load_apiary_context) or
synthetic rules dirs for multi-rule scenarios.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

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


def _write_rule(path: Path, rule_id: str, body: str, category: str = "behavioral") -> None:
    path.write_text(
        f"---\nid: {rule_id}\ntitle: {rule_id}\ncategory: {category}\nrequires: []\n---\n{body}\n",
        encoding="utf-8",
    )


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

    def _synthetic_rules_dir(self) -> Path:
        """Create a synthetic rules dir with two rules for multi-rule tests."""
        rules_dir = Path(self._tmp.name) / "rules" / "behavioral"
        rules_dir.mkdir(parents=True)
        _write_rule(rules_dir / "alpha.md", "alpha", "Alpha body content.")
        _write_rule(rules_dir / "beta.md", "beta", "Beta body content.")
        return rules_dir.parent

    def _synthetic_common(self, rules_dir: Path, *extra: str) -> list[str]:
        return ["--target", str(self.target), "--rules-dir", str(rules_dir), *extra]


class TestInstallAll(InstallerTestCase):
    def test_fresh_install(self):
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, 0)
        text = self._read()
        self.assertIn(cr.OUTER_START, text)
        self.assertIn(cr.OUTER_END, text)
        zone = cr.find_managed_zone(text)
        ids = {ir_.id for ir_ in zone.rules}
        self.assertIn("load_apiary_context", ids)

    def test_fresh_install_synthetic(self):
        rules_dir = self._synthetic_rules_dir()
        result = run(*self._synthetic_common(rules_dir, "--install-all"))
        self.assertEqual(result.exit_code, 0)
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertEqual(ids, {"alpha", "beta"})

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
        rules_dir = self._synthetic_rules_dir()
        run(*self._synthetic_common(rules_dir, "--install-all"))
        run(*self._synthetic_common(rules_dir, "--uninstall", "alpha"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertNotIn("alpha", ids)
        self.assertIn("beta", ids)

    def test_uninstall_last_removes_zone(self):
        run(*self._common("--install", "load_apiary_context"))
        run(*self._common("--uninstall", "load_apiary_context"))
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

    def test_no_zone_reports_missing(self):
        self._write("# nothing here\n")
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, ir.EXIT_ZONE_MISSING)
        self.assertIn("zone missing", result.stdout)

    def test_empty_file_reports_missing(self):
        self._write("")
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, ir.EXIT_ZONE_MISSING)
        self.assertIn("zone missing", result.stdout)


class TestTamperEnforcement(InstallerTestCase):
    # A sentinel that won't collide with any rule body. Inserted inside the
    # managed rule body so the stored hash no longer matches — wording-
    # independent, so a reworded rule body never breaks these tests.
    _TAMPER = "HAND-EDITED-BY-TEST"

    @classmethod
    def _tamper_body(cls, text: str) -> str:
        """Insert a sentinel line inside the first managed rule body.

        Lands between the rule's open ``hash=`` marker and its closing marker,
        which is exactly the region ``compute_drift`` re-hashes, so the edit is
        detected as a body tamper regardless of the rule's prose.
        """
        open_marker = "<!-- apiary-context-rule:"
        start = text.index(open_marker)
        body_start = text.index("-->", start) + len("-->")
        return text[:body_start] + f"\n{cls._TAMPER}" + text[body_start:]

    def test_body_tamper_blocks_install(self):
        run(*self._common("--install-all"))
        self._write(self._tamper_body(self._read()))
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)
        self.assertIn("hand-edited", result.stderr)

    def test_force_overrides_tamper(self):
        run(*self._common("--install-all"))
        self._write(self._tamper_body(self._read()))
        result = run(*self._common("--install-all", "--force"))
        self.assertEqual(result.exit_code, 0)
        # --force rebuilds the zone from source: the tamper is gone and a fresh
        # --check passes (canonical content restored), without asserting prose.
        self.assertNotIn(self._TAMPER, self._read())
        self.assertEqual(run(*self._common("--check")).exit_code, 0)

    def test_zone_tamper_blocks(self):
        run(*self._common("--install-all"))
        # Delete the end marker to break the zone.
        broken = self._read().replace(cr.OUTER_END, "")
        self._write(broken)
        result = run(*self._common("--install-all"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)

    def test_check_reports_tamper_exit_code(self):
        run(*self._common("--install-all"))
        self._write(self._tamper_body(self._read()))
        result = run(*self._common("--check"))
        self.assertEqual(result.exit_code, ir.EXIT_TAMPER)
        self.assertIn("TAMPERED", result.stdout)


class TestReplaceStopgap(InstallerTestCase):
    """Test the stopgap-stripping mechanism using synthetic markers."""

    def test_strips_known_stopgap_paragraphs(self):
        # Patch STOPGAP_MARKERS with synthetic markers for testing
        synthetic_markers = (
            ("alpha", "unique alpha marker phrase"),
            ("beta", "unique beta marker phrase"),
        )
        rules_dir = self._synthetic_rules_dir()
        stopgap = (
            "# Global Rules\n\n"
            "## Old section\n\n"
            "This has unique alpha marker phrase in it.\n\n"
            "## Another old section\n\n"
            "This has unique beta marker phrase in it.\n\n"
            "## Keep this\n\n"
            "Unrelated content.\n"
        )
        self._write(stopgap)
        with mock.patch.object(ir, "STOPGAP_MARKERS", synthetic_markers):
            result = run(*self._synthetic_common(rules_dir, "--install-all", "--replace-stopgap"))
        self.assertEqual(result.exit_code, 0)
        text = self._read()
        before_zone = text.split(cr.OUTER_START)[0]
        self.assertNotIn("unique alpha marker phrase", before_zone)
        self.assertNotIn("unique beta marker phrase", before_zone)
        self.assertIn("Keep this", text)
        self.assertIn("Unrelated content.", text)

    def test_does_not_strip_inside_existing_zone(self):
        # Use a synthetic rule whose body contains a marker phrase.
        rules_dir = Path(self._tmp.name) / "rules" / "behavioral"
        rules_dir.mkdir(parents=True)
        _write_rule(rules_dir / "alpha.md", "alpha", "Body with unique alpha marker phrase.")
        rd = rules_dir.parent
        synthetic_markers = (("alpha", "unique alpha marker phrase"),)

        with mock.patch.object(ir, "STOPGAP_MARKERS", synthetic_markers):
            run(*self._synthetic_common(rd, "--install-all"))
            before = self._read()
            run(*self._synthetic_common(rd, "--install-all", "--replace-stopgap"))
            after = self._read()
        # The rule body inside the zone survived.
        self.assertIn("unique alpha marker phrase", after)
        self.assertEqual(after, before)

    def test_strips_full_section_when_only_one_paragraph_matches(self):
        synthetic_markers = (("alpha", "unique alpha marker phrase"),)
        rules_dir = self._synthetic_rules_dir()
        stopgap = (
            "# Global Rules\n\n"
            "### Alpha section\n\n"
            "First paragraph with unique alpha marker phrase.\n\n"
            "Second paragraph that belongs to the same section.\n\n"
            "### Other section\n\n"
            "Keep this entirely.\n"
        )
        self._write(stopgap)
        with mock.patch.object(ir, "STOPGAP_MARKERS", synthetic_markers):
            result = run(*self._synthetic_common(rules_dir, "--install-all", "--replace-stopgap"))
        self.assertEqual(result.exit_code, 0)
        before_zone = self._read().split(cr.OUTER_START)[0]
        self.assertNotIn("Alpha section", before_zone)
        self.assertNotIn("unique alpha marker phrase", before_zone)
        self.assertNotIn("Second paragraph", before_zone)
        self.assertIn("### Other section", before_zone)
        self.assertIn("Keep this entirely.", before_zone)

    def test_preserves_unrelated_sections_with_marker_neighbors(self):
        synthetic_markers = (("alpha", "unique alpha marker phrase"),)
        rules_dir = self._synthetic_rules_dir()
        stopgap = (
            "# Global Rules\n\n"
            "### Unrelated section\n\n"
            "This paragraph stays.\n\n"
            "And so does this one.\n\n"
            "### Alpha rule\n\n"
            "Contains unique alpha marker phrase.\n\n"
            "**Why:** Some reason.\n"
        )
        self._write(stopgap)
        with mock.patch.object(ir, "STOPGAP_MARKERS", synthetic_markers):
            result = run(*self._synthetic_common(rules_dir, "--install-all", "--replace-stopgap"))
        self.assertEqual(result.exit_code, 0)
        before_zone = self._read().split(cr.OUTER_START)[0]
        self.assertIn("### Unrelated section", before_zone)
        self.assertIn("This paragraph stays.", before_zone)
        self.assertIn("And so does this one.", before_zone)
        self.assertNotIn("Alpha rule", before_zone)

    def test_pre_header_preamble_falls_back_to_paragraph_strip(self):
        synthetic_markers = (("alpha", "unique alpha marker phrase"),)
        rules_dir = self._synthetic_rules_dir()
        stopgap = (
            "# Global Rules\n\n"
            "Quick note: unique alpha marker phrase here.\n\n"
            "Some other unrelated paragraph that should survive.\n\n"
            "### Real section\n\n"
            "Body of the real section.\n"
        )
        self._write(stopgap)
        with mock.patch.object(ir, "STOPGAP_MARKERS", synthetic_markers):
            result = run(*self._synthetic_common(rules_dir, "--install-all", "--replace-stopgap"))
        self.assertEqual(result.exit_code, 0)
        before_zone = self._read().split(cr.OUTER_START)[0]
        self.assertNotIn("unique alpha marker phrase", before_zone)
        self.assertIn("Some other unrelated paragraph", before_zone)
        self.assertIn("### Real section", before_zone)
        self.assertIn("Body of the real section.", before_zone)


class TestList(InstallerTestCase):
    def test_list_runs_clean(self):
        result = run(*self._common("--list"))
        self.assertEqual(result.exit_code, 0)
        self.assertIn("load_apiary_context", result.stdout)
        self.assertIn("not installed", result.stdout)

    def test_list_after_install(self):
        run(*self._common("--install-all"))
        result = run(*self._common("--list"))
        self.assertIn("installed", result.stdout)


class TestDiff(InstallerTestCase):
    def test_diff_no_change(self):
        run(*self._common("--install-all"))
        result = run(*self._common("--diff", "load_apiary_context"))
        self.assertEqual(result.exit_code, 0)
        # No diff lines when bodies match
        self.assertEqual(result.stdout, "")

    def test_diff_unknown_rule(self):
        result = run(*self._common("--diff", "no_such_rule"))
        self.assertEqual(result.exit_code, ir.EXIT_USAGE)


class TestInstallSpecific(InstallerTestCase):
    def test_install_one(self):
        run(*self._common("--install", "load_apiary_context"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertEqual(ids, {"load_apiary_context"})

    def test_install_unknown(self):
        result = run(*self._common("--install", "no_such_rule"))
        self.assertEqual(result.exit_code, ir.EXIT_USAGE)

    def test_install_category(self):
        run(*self._common("--install-category", "behavioral"))
        zone = cr.find_managed_zone(self._read())
        ids = {ir_.id for ir_ in zone.rules}
        self.assertIn("load_apiary_context", ids)


if __name__ == "__main__":
    unittest.main()
