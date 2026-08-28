#!/usr/bin/env python3
"""Tests for runner/schedulers/windows.py.

Uses subprocess mocks so these tests run on any OS (the Windows backend
only imports stdlib at module load; native schtasks calls are stubbed).
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from runner.schedulers import windows as win_mod
from runner.schedulers.base import SchedulerError

_CSV_SAMPLE = (
    '"HostName","TaskName","Task To Run","Start In","Schedule Type","Start Time"\r\n'
    '"HOST","\\apiary\\overnight","python -m runner.run --detached",'
    '"D:\\apiary","Daily","2:00:00 AM"\r\n'
    '"HostName","TaskName","Task To Run","Start In","Schedule Type","Start Time"\r\n'
    '"HOST","\\OtherUser\\Backup","backup.exe","C:\\Backup","Daily","3:00:00 AM"\r\n'
    '"HostName","TaskName","Task To Run","Start In","Schedule Type","Start Time"\r\n'
    '"HOST","\\apiary\\","","","",""\r\n'
)


def _fake_run_ok(stdout: str = "", stderr: str = ""):
    return mock.Mock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=stdout,
            stderr=stderr,
        )
    )


def _fake_run_fail(stderr: str = "boom"):
    return mock.Mock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=stderr,
        )
    )


class ListEntriesTests(unittest.TestCase):
    def test_filters_to_prefix_and_skips_prefix_folder(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_ok(_CSV_SAMPLE)):
            entries = backend.list_entries("\\apiary\\")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry_id, "overnight")
        # command parsed back to list form
        self.assertEqual(
            entries[0].command,
            ["python", "-m", "runner.run", "--detached"],
        )
        self.assertEqual(entries[0].cwd, "D:\\apiary")
        self.assertEqual(entries[0].schedule["type"], "daily")
        self.assertEqual(entries[0].schedule["time"], "02:00")

    def test_non_apiary_entries_not_returned(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_ok(_CSV_SAMPLE)):
            entries = backend.list_entries("\\apiary\\")
        ids = [e.entry_id for e in entries]
        self.assertNotIn("Backup", ids)
        self.assertNotIn("\\OtherUser\\Backup", ids)

    def test_n_a_start_in_becomes_empty_cwd(self):
        """schtasks reports tasks with no working dir as 'N/A' — we
        normalize that to an empty string so the registry's empty cwd
        compares equal to the observed entry."""
        csv = (
            '"HostName","TaskName","Task To Run","Start In","Schedule Type","Start Time"\r\n'
            '"HOST","\\apiary\\no-cwd","python -m runner.run",'
            '"N/A","Daily","2:00:00 AM"\r\n'
        )
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_ok(csv)):
            entries = backend.list_entries("\\apiary\\")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].cwd, "")

    def test_backslash_paths_in_task_to_run_preserved(self):
        """shlex posix=True would eat Windows backslashes; posix=False
        keeps them. A task whose command references an absolute Windows
        path must round-trip with backslashes intact."""
        csv = (
            '"HostName","TaskName","Task To Run","Start In","Schedule Type","Start Time"\r\n'
            '"HOST","\\apiary\\abs","C:\\Python\\python.exe -m runner.run",'
            '"D:\\apiary","Daily","2:00:00 AM"\r\n'
        )
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_ok(csv)):
            entries = backend.list_entries("\\apiary\\")
        self.assertEqual(
            entries[0].command,
            ["C:\\Python\\python.exe", "-m", "runner.run"],
        )

    def test_empty_output_returns_empty_list(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_ok("")):
            self.assertEqual(backend.list_entries("\\apiary\\"), [])

    def test_schtasks_failure_raises(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_fail("denied")):
            with self.assertRaises(SchedulerError) as ctx:
                backend.list_entries("\\apiary\\")
        self.assertIn("denied", ctx.exception.stderr)


class CreateAndDeleteTests(unittest.TestCase):
    def test_create_passes_xml_file_to_schtasks(self):
        backend = win_mod.WindowsTaskScheduler()
        fake = _fake_run_ok()
        with mock.patch.object(backend, "_run", fake):
            backend.create_entry(
                "\\apiary\\",
                "overnight",
                ["python", "-m", "runner.run", "--detached"],
                "D:\\apiary",
                {"type": "daily", "time": "02:00"},
            )
        args = fake.call_args[0][0]
        self.assertIn("/Create", args)
        self.assertIn("/TN", args)
        self.assertIn("\\apiary\\overnight", args)
        self.assertIn("/XML", args)
        self.assertIn("/F", args)

    def test_create_xml_body_includes_command_args_and_cwd(self):
        body = win_mod._build_task_xml(
            ["python", "-m", "runner.run", "--detached"],
            "D:\\apiary",
            {"type": "daily", "time": "02:00"},
        )
        self.assertIn("<Command>python</Command>", body)
        self.assertIn(
            "<Arguments>-m runner.run --detached</Arguments>",
            body,
        )
        self.assertIn("<WorkingDirectory>D:\\apiary</WorkingDirectory>", body)
        self.assertIn("<StartBoundary>2026-01-01T02:00:00</StartBoundary>", body)
        self.assertIn("<ScheduleByDay>", body)

    def test_create_xml_body_omits_workingdirectory_when_cwd_empty(self):
        body = win_mod._build_task_xml(
            ["python", "-m", "x"],
            "",
            {"type": "daily", "time": "02:00"},
        )
        self.assertNotIn("<WorkingDirectory>", body)

    def test_create_xml_body_escapes_special_chars(self):
        body = win_mod._build_task_xml(
            ["python & go", "-c", "print('<hi>')"],
            "D:\\dir",
            {"type": "daily", "time": "02:00"},
        )
        # XML escaping: & → &amp;, < → &lt;, > → &gt;
        self.assertIn("&amp;", body)
        self.assertIn("&lt;", body)
        self.assertIn("&gt;", body)

    def test_create_unsupported_schedule_raises(self):
        backend = win_mod.WindowsTaskScheduler()
        fake = _fake_run_ok()
        with mock.patch.object(backend, "_run", fake):
            with self.assertRaises(SchedulerError) as ctx:
                backend.create_entry(
                    "\\apiary\\",
                    "x",
                    ["python"],
                    "D:\\",
                    {"type": "weekly"},
                )
        self.assertIn("not supported", str(ctx.exception))
        fake.assert_not_called()  # guard ran before any schtasks call

    def test_create_empty_command_raises(self):
        with self.assertRaises(SchedulerError):
            win_mod._build_task_xml(
                [],
                "D:\\",
                {"type": "daily", "time": "02:00"},
            )

    def test_create_failure_raises_with_stderr(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_fail("access denied")):
            with self.assertRaises(SchedulerError) as ctx:
                backend.create_entry(
                    "\\apiary\\",
                    "x",
                    ["python"],
                    "D:\\",
                    {"type": "daily", "time": "02:00"},
                )
        self.assertIn("access denied", ctx.exception.stderr)

    def test_delete_passes_name_and_force(self):
        backend = win_mod.WindowsTaskScheduler()
        fake = _fake_run_ok()
        with mock.patch.object(backend, "_run", fake):
            backend.delete_entry("\\apiary\\", "overnight")
        args = fake.call_args[0][0]
        self.assertIn("/Delete", args)
        self.assertIn("\\apiary\\overnight", args)
        self.assertIn("/f", args)

    def test_delete_failure_raises_with_stderr(self):
        backend = win_mod.WindowsTaskScheduler()
        with mock.patch.object(backend, "_run", _fake_run_fail("not found")):
            with self.assertRaises(SchedulerError) as ctx:
                backend.delete_entry("\\apiary\\", "x")
        self.assertIn("not found", ctx.exception.stderr)


class CanonicalTimeTests(unittest.TestCase):
    def test_am_conversion(self):
        self.assertEqual(win_mod._canonical_time("2:00:00 AM"), "02:00")

    def test_pm_conversion(self):
        self.assertEqual(win_mod._canonical_time("2:30:00 PM"), "14:30")

    def test_twelve_am_is_midnight(self):
        self.assertEqual(win_mod._canonical_time("12:00:00 AM"), "00:00")

    def test_twelve_pm_is_noon(self):
        self.assertEqual(win_mod._canonical_time("12:00:00 PM"), "12:00")

    def test_already_24h(self):
        self.assertEqual(win_mod._canonical_time("14:30:00"), "14:30")

    def test_empty_string(self):
        self.assertEqual(win_mod._canonical_time(""), "")


class XmlArgQuoteTests(unittest.TestCase):
    def test_no_spaces_unchanged(self):
        self.assertEqual(win_mod._xml_arg_quote("python"), "python")

    def test_spaces_get_quoted(self):
        self.assertEqual(
            win_mod._xml_arg_quote("Program Files\\Python"),
            '"Program Files\\Python"',
        )

    def test_inner_quote_escaped(self):
        self.assertEqual(win_mod._xml_arg_quote('a"b'), '"a\\"b"')

    def test_empty_string_quoted(self):
        self.assertEqual(win_mod._xml_arg_quote(""), '""')


if __name__ == "__main__":
    unittest.main()
