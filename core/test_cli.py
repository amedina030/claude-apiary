"""Tests for ``core/cli.py`` — the ``apiary`` console script's argv contract.

Every verb and sub-verb is exercised through ``cli.main([...])`` with the
implementation module mocked out, so these tests assert exactly one thing:
that a given argv reaches the right function with the right arguments. The
implementations have their own behavioural tests (``test_install.py``,
``test_uninstall.py``, ``test_doctor.py``, …) — nothing here re-tests them.

The one exception is the ``doctor … --fix`` pair, which runs through the real
``doctor.main`` with only the writer mocked. ``--fix`` has to survive *two*
argument parsers (``apiary``'s and ``doctor``'s), and it silently did not:
``apiary doctor pointers --fix`` failed with "unrecognized arguments: --fix"
while three docs told users to run it. Mocking ``doctor.main`` alone would not
have caught that, so the seam itself is covered end to end.
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cascade, cli, doctor


def _main(argv: list[str]) -> tuple[int, str]:
    """Run ``cli.main(argv)`` with stdout captured. Returns ``(rc, stdout)``."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def _expect_usage_error(testcase: unittest.TestCase, argv: list[str]) -> None:
    """Assert *argv* is rejected by argparse (exit 2), swallowing its stderr."""
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        with testcase.assertRaises(SystemExit) as ctx:
            cli.main(argv)
    testcase.assertEqual(ctx.exception.code, 2)


class InstallVerbTests(unittest.TestCase):
    def test_target_is_forwarded_with_default_profile(self):
        with mock.patch("core.install.install") as m:
            rc, _ = _main(["install", "--target", "some/repo"])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(Path("some/repo"), profile="base", apiary_repo=None)

    def test_profile_and_apiary_repo_are_forwarded(self):
        with mock.patch("core.install.install") as m:
            rc, _ = _main([
                "install", "--target", "some/repo",
                "--profile", "python", "--apiary-repo", "main/apiary",
            ])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(
            Path("some/repo"), profile="python", apiary_repo=Path("main/apiary"),
        )

    def test_target_is_required(self):
        _expect_usage_error(self, ["install"])


class UninstallVerbTests(unittest.TestCase):
    def test_defaults_keep_the_data(self):
        with mock.patch("core.uninstall.uninstall") as m:
            rc, _ = _main(["uninstall", "--target", "some/repo"])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(Path("some/repo"), apiary_repo=None, remove_data=False)

    def test_remove_data_is_forwarded(self):
        with mock.patch("core.uninstall.uninstall") as m:
            rc, _ = _main([
                "uninstall", "--target", "some/repo",
                "--remove-data", "--apiary-repo", "main/apiary",
            ])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(
            Path("some/repo"), apiary_repo=Path("main/apiary"), remove_data=True,
        )

    def test_target_is_required(self):
        _expect_usage_error(self, ["uninstall"])


class SelfBootstrapVerbTests(unittest.TestCase):
    def test_no_args_passes_none(self):
        with mock.patch("core.self_bootstrap.self_bootstrap") as m:
            rc, _ = _main(["self-bootstrap"])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(None)

    def test_apiary_repo_is_forwarded_positionally(self):
        with mock.patch("core.self_bootstrap.self_bootstrap") as m:
            rc, _ = _main(["self-bootstrap", "--apiary-repo", "main/apiary"])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(Path("main/apiary"))


class DoctorVerbTests(unittest.TestCase):
    """``apiary doctor`` builds a doctor argv; assert it byte for byte."""

    def _forwarded(self, argv: list[str], rc_from_doctor: int = 0) -> tuple[int, list[str]]:
        with mock.patch("core.doctor.main", return_value=rc_from_doctor) as m:
            rc, _ = _main(argv)
        m.assert_called_once()
        return rc, m.call_args.args[0]

    def test_bare_doctor_runs_every_check(self):
        rc, forwarded = self._forwarded(["doctor"])
        self.assertEqual(rc, 0)
        self.assertEqual(forwarded, [])

    def test_every_registered_check_is_accepted_and_forwarded(self):
        for name in doctor.CHECKS:
            with self.subTest(check=name):
                _, forwarded = self._forwarded(["doctor", name])
                self.assertEqual(forwarded, [name])

    def test_unknown_check_is_a_usage_error(self):
        _expect_usage_error(self, ["doctor", "not-a-check"])

    def test_fix_is_forwarded_with_the_check(self):
        for name in doctor.FIXES:
            with self.subTest(check=name):
                _, forwarded = self._forwarded(["doctor", name, "--fix"])
                self.assertEqual(forwarded, [name, "--fix"])

    def test_fix_without_a_check_is_forwarded_for_doctor_to_reject(self):
        # `apiary` does not duplicate doctor's "which checks have fixes"
        # knowledge; it hands `--fix` over and doctor emits the error (rc 2).
        _, forwarded = self._forwarded(["doctor", "--fix"], rc_from_doctor=2)
        self.assertEqual(forwarded, ["--fix"])

    def test_check_fix_and_apiary_repo_are_forwarded_in_order(self):
        _, forwarded = self._forwarded([
            "doctor", "mailbox", "--fix", "--apiary-repo", "main/apiary",
        ])
        self.assertEqual(forwarded, ["mailbox", "--fix", "--apiary-repo", str(Path("main/apiary"))])

    def test_doctor_exit_code_is_propagated(self):
        rc, _ = self._forwarded(["doctor", "registry"], rc_from_doctor=1)
        self.assertEqual(rc, 1)


class DoctorFixSeamTests(unittest.TestCase):
    """End-to-end through the real ``doctor.main``: only the writers are mocked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apiary = Path(self._tmp.name).resolve()

    def test_pointers_fix_reaches_the_cascade_writer(self):
        report = cascade.CascadeReport(
            new_main_apiary_path=self.apiary, updated=[], skipped=[],
        )
        with mock.patch("core.cascade.cascade_fix", return_value=report) as m:
            rc, out = _main([
                "doctor", "pointers", "--fix", "--apiary-repo", str(self.apiary),
            ])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(self.apiary)
        self.assertIn("[pointers --fix]", out)

    def test_mailbox_fix_reaches_the_mailbox_writer(self):
        with mock.patch(
            "core.mailbox.process_pending",
            return_value={"processed": 0, "applied": [], "errors": []},
        ) as m:
            rc, out = _main([
                "doctor", "mailbox", "--fix", "--apiary-repo", str(self.apiary),
            ])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(self.apiary)
        self.assertIn("[mailbox --fix]", out)

    def test_fix_on_a_check_without_a_writer_exits_2(self):
        rc, _ = _main([
            "doctor", "registry", "--fix", "--apiary-repo", str(self.apiary),
        ])
        self.assertEqual(rc, 2)

    def test_fix_without_a_check_exits_2(self):
        rc, _ = _main(["doctor", "--fix", "--apiary-repo", str(self.apiary)])
        self.assertEqual(rc, 2)


class MailboxVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apiary = Path(self._tmp.name)
        patcher = mock.patch(
            "core.utils.state.resolve_apiary_repo", return_value=self.apiary,
        )
        self.resolve = patcher.start()
        self.addCleanup(patcher.stop)

    def test_bare_mailbox_drains_the_queue(self):
        with mock.patch("core.mailbox.process_pending",
                        return_value={"processed": 1, "applied": [], "errors": []}) as proc, \
             mock.patch("core.mailbox.list_pending") as listing:
            rc, out = _main(["mailbox"])
        self.assertEqual(rc, 0)
        proc.assert_called_once_with(self.apiary)
        listing.assert_not_called()
        self.assertIn("processed 1 message(s)", out)

    def test_process_errors_exit_nonzero(self):
        with mock.patch(
            "core.mailbox.process_pending",
            return_value={"processed": 1, "applied": [],
                          "errors": [{"file": "a.json", "reason": "bad"}]},
        ):
            rc, out = _main(["mailbox"])
        self.assertEqual(rc, 1)
        self.assertIn("a.json", out)

    def test_list_inspects_without_draining(self):
        msg_path = self.apiary / "0001.json"
        with mock.patch("core.mailbox.list_pending", return_value=[msg_path]) as listing, \
             mock.patch("core.mailbox.read_message",
                        return_value={"kind": "update_path", "from_uid": 7,
                                      "new_path": "/elsewhere"}), \
             mock.patch("core.mailbox.process_pending") as proc:
            rc, out = _main(["mailbox", "--list"])
        self.assertEqual(rc, 0)
        listing.assert_called_once_with(self.apiary)
        proc.assert_not_called()
        self.assertIn("1 pending message(s)", out)
        self.assertIn("update_path", out)

    def test_list_tolerates_a_malformed_message(self):
        with mock.patch("core.mailbox.list_pending",
                        return_value=[self.apiary / "0001.json"]), \
             mock.patch("core.mailbox.read_message", return_value=None):
            rc, out = _main(["mailbox", "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("<malformed>", out)

    def test_apiary_repo_is_forwarded_to_the_resolver(self):
        with mock.patch("core.mailbox.process_pending",
                        return_value={"processed": 0, "applied": [], "errors": []}):
            rc, _ = _main(["mailbox", "--apiary-repo", "main/apiary"])
        self.assertEqual(rc, 0)
        self.resolve.assert_called_once_with(Path("main/apiary"))


class CascadeFixVerbTests(unittest.TestCase):
    def test_cascade_fix_runs_against_the_resolved_apiary(self):
        with tempfile.TemporaryDirectory() as tmp:
            apiary = Path(tmp)
            report = cascade.CascadeReport(
                new_main_apiary_path=apiary.resolve(), updated=[3], skipped=[(4, "gone")],
            )
            with mock.patch("core.utils.state.resolve_apiary_repo",
                            return_value=apiary) as resolve, \
                 mock.patch("core.cascade.cascade_fix", return_value=report) as m:
                rc, out = _main(["cascade-fix", "--apiary-repo", str(apiary)])
            resolve.assert_called_once_with(apiary)
            m.assert_called_once_with(apiary.resolve())
        self.assertEqual(rc, 0)
        self.assertIn("updated 1 repo(s); skipped 1", out)
        self.assertIn("skipped uid=4: gone", out)


class VersionVerbTests(unittest.TestCase):
    def test_version_prints_the_pinned_version(self):
        with mock.patch("core.utils.state.resolve_apiary_repo",
                        return_value=Path("main/apiary")) as resolve, \
             mock.patch("core.utils.state.read_apiary_version", return_value="0.9.1") as read:
            rc, out = _main(["version", "--apiary-repo", "main/apiary"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "0.9.1")
        resolve.assert_called_once_with(Path("main/apiary"))
        read.assert_called_once_with(Path("main/apiary"))


class ParserContractTests(unittest.TestCase):
    def test_a_subcommand_is_required(self):
        _expect_usage_error(self, [])

    def test_unknown_verb_is_a_usage_error(self):
        _expect_usage_error(self, ["frobnicate"])

    def test_every_documented_verb_is_registered(self):
        # Locks the verb list so a rename cannot land unnoticed. This is the
        # same surface docs/check_cli_claims.py reconciles the "## apiary"
        # section of docs/reference/cli-tools.md against.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            cli.main(["--help"])
        match = re.search(r"\{([^}]*)\}", buf.getvalue(), re.S)
        self.assertIsNotNone(match, "no subcommand metavar in `apiary --help`")
        self.assertEqual(
            {s.strip() for s in match.group(1).split(",")},
            {"install", "uninstall", "self-bootstrap", "doctor",
             "mailbox", "cascade-fix", "version"},
        )


if __name__ == "__main__":
    unittest.main()
