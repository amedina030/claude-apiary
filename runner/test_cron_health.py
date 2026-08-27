#!/usr/bin/env python3
"""Tests for runner/cron_health.py."""
from __future__ import annotations
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import cron_health
from runner.schedulers.base import (
    ObservedEntry, SchedulerBackend, SchedulerError, UnsupportedPlatformError,
)


def _write_registry(tmp: Path, entries: list[dict]) -> Path:
    p = tmp / "cron_registry.json"
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return p


class HostnameRegistryPathTests(unittest.TestCase):
    """registry_path_for_host returns <apiary>/cron_registry/<hostname>.json."""

    def test_explicit_hostname_used_in_path(self):
        path = cron_health.registry_path_for_host("my-laptop")
        self.assertEqual(path.name, "my-laptop.json")
        self.assertEqual(path.parent, cron_health.CRON_REGISTRY_DIR)

    def test_hostname_defaults_to_platform_node(self):
        import platform
        path = cron_health.registry_path_for_host()
        expected = platform.node().strip() or "unknown"
        # Don't assert exact characters (the sanitiser may swap some) —
        # just verify the file lives under CRON_REGISTRY_DIR and has a name.
        self.assertEqual(path.parent, cron_health.CRON_REGISTRY_DIR)
        self.assertTrue(path.name.endswith(".json"))
        self.assertTrue(len(path.stem) > 0)
        # And that the computed hostname is not literally "platform.node()"
        # (catches a dumb copy-paste bug).
        self.assertNotIn(".", path.stem[1:] + " ")

    def test_hostname_sanitises_illegal_chars(self):
        # Spaces and path separators get replaced with '-'.
        with mock.patch("platform.node", return_value="weird host/name"):
            name = cron_health._hostname()
        self.assertNotIn("/", name)
        self.assertNotIn(" ", name)

    def test_empty_hostname_falls_back_to_unknown(self):
        with mock.patch("platform.node", return_value=""):
            self.assertEqual(cron_health._hostname(), "unknown")


class CommandPlaceholderTests(unittest.TestCase):
    """RegistryEntry.from_raw resolves <python> / <apiary_repo> in commands so
    a registry stays portable instead of hardcoding a bare interpreter name."""

    APIARY_ROOT = Path("/apiary")

    def _command_of(self, command: list[str]) -> list[str]:
        raw = {
            "id": "x", "command": command, "cwd": "<apiary_repo>",
            "schedule": {"type": "daily", "time": "02:00"},
        }
        return cron_health.RegistryEntry.from_raw(raw, self.APIARY_ROOT).command

    def test_python_placeholder_resolves_to_running_interpreter(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APIARY_PYTHON", None)
            command = self._command_of(["<python>", "-m", "runner.run"])
        self.assertEqual(Path(command[0]), Path(sys.executable))
        self.assertEqual(command[1:], ["-m", "runner.run"])

    def test_python_placeholder_honors_override(self):
        override = "/opt/py3/bin/python3"
        with mock.patch.dict(os.environ, {"APIARY_PYTHON": override}):
            command = self._command_of(["<python>", "-m", "runner.run"])
        self.assertEqual(Path(command[0]), Path(override))

    def test_literal_python_token_left_unchanged(self):
        # Legacy registries that hardcode "python" must keep working.
        self.assertEqual(
            self._command_of(["python", "-m", "runner.run"]),
            ["python", "-m", "runner.run"],
        )

    def test_apiary_repo_placeholder_in_command(self):
        command = self._command_of(["<python>", "<apiary_repo>/x.py"])
        self.assertEqual(command[1], f"{self.APIARY_ROOT}/x.py")


class FakeBackend(SchedulerBackend):
    """In-memory scheduler for tests — no subprocess, no real schtasks."""

    name = "fake"

    def __init__(self, entries: list[ObservedEntry] | None = None):
        self._entries: dict[str, ObservedEntry] = {
            e.entry_id: e for e in (entries or [])
        }
        self.created: list[tuple[str, str, list[str], str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_delete_ids: set[str] = set()
        self.fail_create_ids: set[str] = set()

    def list_entries(self, prefix: str) -> list[ObservedEntry]:
        return list(self._entries.values())

    def create_entry(self, prefix, entry_id, command, cwd, schedule) -> None:
        self.created.append((prefix, entry_id, command, cwd, schedule))
        if entry_id in self.fail_create_ids:
            raise SchedulerError("simulated create failure", stderr="nope")
        self._entries[entry_id] = ObservedEntry(
            entry_id=entry_id, command=command, cwd=cwd, schedule=schedule,
        )

    def delete_entry(self, prefix, entry_id) -> None:
        self.deleted.append((prefix, entry_id))
        if entry_id in self.fail_delete_ids:
            raise SchedulerError("simulated delete failure", stderr="nope")
        self._entries.pop(entry_id, None)


class LoadRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = self.tmp / "apiary"
        self.apiary.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_raises(self):
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(
                self.tmp / "nope.json", apiary_root=self.apiary,
            )
        self.assertIn("registry not found", str(ctx.exception))

    def test_malformed_json_raises_with_location(self):
        p = self.tmp / "bad.json"
        p.write_text("{ not valid", encoding="utf-8")
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertIn("not valid JSON", str(ctx.exception))
        self.assertIn("line", str(ctx.exception))

    def test_entries_must_be_list(self):
        p = self.tmp / "bad.json"
        p.write_text(json.dumps({"entries": "nope"}), encoding="utf-8")
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertIn("must be a list", str(ctx.exception))

    def test_entry_must_have_id(self):
        p = _write_registry(self.tmp, [{"command": ["python"]}])
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertIn("missing 'id'", str(ctx.exception))

    def test_entry_must_have_command(self):
        p = _write_registry(self.tmp, [{"id": "x"}])
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertIn("non-empty 'command' list", str(ctx.exception))

    def test_duplicate_ids_rejected(self):
        p = _write_registry(self.tmp, [
            {"id": "a", "command": ["python"]},
            {"id": "a", "command": ["python"]},
        ])
        with self.assertRaises(cron_health.RegistryError) as ctx:
            cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertIn("duplicate entry id", str(ctx.exception))

    def test_placeholder_resolution_in_cwd(self):
        p = _write_registry(self.tmp, [{
            "id": "x",
            "command": ["python", "-m", "runner.run"],
            "cwd": "<apiary_repo>",
        }])
        entries = cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertEqual(entries[0].cwd, str(self.apiary))

    def test_empty_entries_list_ok(self):
        p = _write_registry(self.tmp, [])
        entries = cron_health.load_registry(p, apiary_root=self.apiary)
        self.assertEqual(entries, [])


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = self.tmp / "apiary"
        self.apiary.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, **over) -> cron_health.RegistryEntry:
        base = dict(
            entry_id="x",
            description="",
            command=["python", "-m", "runner.run"],
            cwd=str(self.apiary),
            schedule={"type": "daily", "time": "02:00"},
            disabled=False,
        )
        base.update(over)
        return cron_health.RegistryEntry(**base)

    def _observed(self, entry, **over):
        base = dict(
            entry_id=entry.entry_id,
            command=entry.command,
            cwd=entry.cwd,
            schedule=entry.schedule,
        )
        base.update(over)
        return ObservedEntry(**base)

    def test_ok_when_matching(self):
        e = self._entry()
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "ok")

    def test_missing_when_no_observed(self):
        e = self._entry()
        status = cron_health.classify_entry(e, None)
        self.assertEqual(status.state, "missing")

    def test_command_drift(self):
        e = self._entry()
        obs = self._observed(e, command=["python", "-m", "pipeline.run"])
        status = cron_health.classify_entry(e, obs)
        self.assertEqual(status.state, "broken")
        self.assertEqual(status.reason, "command drift")

    def test_script_path_missing(self):
        e = self._entry(command=["python", "nope_not_a_real_file.py"])
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "broken")
        self.assertEqual(status.reason, "script path missing")

    def test_script_path_present_ok(self):
        script = self.apiary / "real.py"
        script.write_text("", encoding="utf-8")
        e = self._entry(command=["python", str(script)])
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "ok")

    def test_module_invocation_not_probed(self):
        """-m module commands skip the script-path check even if a token
        ends with .py by coincidence — we only scan positional args before
        detecting -m, and -m short-circuits the check."""
        e = self._entry(command=["python", "-m", "runner.run"])
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "ok")

    def test_cwd_missing(self):
        e = self._entry(cwd=str(self.tmp / "does_not_exist"))
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "broken")
        self.assertEqual(status.reason, "cwd missing")

    def test_disabled_with_observed_is_broken(self):
        e = self._entry(disabled=True)
        status = cron_health.classify_entry(e, self._observed(e))
        self.assertEqual(status.state, "broken")
        self.assertIn("should not exist", status.reason)

    def test_disabled_without_observed_is_ok(self):
        e = self._entry(disabled=True)
        status = cron_health.classify_entry(e, None)
        self.assertEqual(status.state, "ok")

    def test_schedule_drift(self):
        e = self._entry()
        obs = self._observed(e, schedule={"type": "daily", "time": "03:00"})
        status = cron_health.classify_entry(e, obs)
        self.assertEqual(status.state, "broken")
        self.assertEqual(status.reason, "schedule drift")

    def test_schedule_type_case_insensitive(self):
        e = self._entry()
        obs = self._observed(e, schedule={"type": "DAILY", "time": "02:00"})
        status = cron_health.classify_entry(e, obs)
        self.assertEqual(status.state, "ok")


class CheckAndRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = self.tmp / "apiary"
        self.apiary.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, **over) -> cron_health.RegistryEntry:
        base = dict(
            entry_id="overnight",
            description="nightly",
            command=["python", "-m", "runner.run", "--detached"],
            cwd=str(self.apiary),
            schedule={"type": "daily", "time": "02:00"},
            disabled=False,
        )
        base.update(over)
        return cron_health.RegistryEntry(**base)

    def test_all_ok_returns_exit_zero(self):
        e = self._entry()
        backend = FakeBackend([ObservedEntry(
            entry_id=e.entry_id, command=e.command,
            cwd=e.cwd, schedule=e.schedule,
        )])
        statuses, rc = cron_health.check([e], backend)
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual([s.state for s in statuses], ["ok"])

    def test_drift_returns_exit_one(self):
        e = self._entry()
        backend = FakeBackend([])
        statuses, rc = cron_health.check([e], backend)
        self.assertEqual(rc, cron_health.EXIT_DRIFT)
        self.assertEqual(statuses[0].state, "missing")

    def test_repair_dry_run_does_not_modify(self):
        e = self._entry()
        backend = FakeBackend([])
        post, actions, rc = cron_health.repair([e], backend, apply=False)
        self.assertEqual(rc, cron_health.EXIT_OK)  # dry-run itself succeeded
        self.assertEqual(backend.created, [])
        self.assertEqual(backend.deleted, [])
        self.assertIn("would create overnight", "\n".join(actions))
        # pre-state echoed back: still missing
        self.assertEqual(post[0].state, "missing")

    def test_repair_apply_creates_missing(self):
        e = self._entry()
        backend = FakeBackend([])
        post, actions, rc = cron_health.repair([e], backend, apply=True)
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual(len(backend.created), 1)
        self.assertEqual(backend.deleted, [])
        self.assertEqual(post[0].state, "ok")
        self.assertIn("created overnight", "\n".join(actions))

    def test_repair_apply_fixes_drift_via_delete_and_recreate(self):
        e = self._entry()
        # Task scheduler has the old pipeline/run.py command
        backend = FakeBackend([ObservedEntry(
            entry_id=e.entry_id,
            command=["python", "-m", "pipeline.run", "--detached"],
            cwd=e.cwd, schedule=e.schedule,
        )])
        post, actions, rc = cron_health.repair([e], backend, apply=True)
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual(len(backend.deleted), 1)
        self.assertEqual(len(backend.created), 1)
        self.assertEqual(post[0].state, "ok")

    def test_repair_apply_deletes_disabled_entry_without_recreate(self):
        e = self._entry(disabled=True)
        backend = FakeBackend([ObservedEntry(
            entry_id=e.entry_id, command=e.command,
            cwd=e.cwd, schedule=e.schedule,
        )])
        post, actions, rc = cron_health.repair([e], backend, apply=True)
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual(len(backend.deleted), 1)
        self.assertEqual(backend.created, [])  # disabled => no recreate
        self.assertEqual(post[0].state, "ok")

    def test_foreign_entries_under_apiary_prefix_ignored(self):
        # backend has an entry whose id isn't in the registry.
        e = self._entry()
        foreign = ObservedEntry(
            entry_id="some-other-task",
            command=["python", "-m", "something.else"],
            cwd=e.cwd, schedule=e.schedule,
        )
        backend = FakeBackend([foreign])
        statuses, rc = cron_health.check([e], backend)
        self.assertEqual(statuses[0].state, "missing")
        self.assertEqual(len(statuses), 1)  # foreign entry not reported
        # repair --apply should not touch the foreign entry
        post, _, _ = cron_health.repair([e], backend, apply=True)
        self.assertEqual(backend.deleted, [])  # only created new one
        # foreign entry still in backend
        ids = [o.entry_id for o in backend.list_entries(cron_health.PREFIX)]
        self.assertIn("some-other-task", ids)

    def test_repair_apply_continues_through_failure(self):
        e1 = self._entry(entry_id="a")
        e2 = self._entry(entry_id="b")
        backend = FakeBackend([])
        backend.fail_create_ids = {"a"}
        post, actions, rc = cron_health.repair([e1, e2], backend, apply=True)
        self.assertEqual(rc, cron_health.EXIT_DRIFT)
        # Second entry was still attempted
        self.assertEqual([c[1] for c in backend.created], ["a", "b"])
        self.assertTrue(
            any("FAILED a" in line for line in actions),
            msg=f"actions: {actions}",
        )


class MainCLITests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = self.tmp / "apiary"
        self.apiary.mkdir()
        self.registry_path = self.tmp / "cron_registry.json"
        self.registry_path.write_text(json.dumps({"entries": [{
            "id": "x",
            "command": ["python", "-m", "runner.run"],
            "cwd": str(self.apiary),
            "schedule": {"type": "daily", "time": "02:00"},
        }]}), encoding="utf-8")
        self._patches = [
            mock.patch.object(cron_health, "REGISTRY_PATH", self.registry_path),
            mock.patch.object(cron_health, "APIARY_REPO_ROOT", self.apiary),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_missing_registry_exits_two(self):
        self.registry_path.unlink()
        with mock.patch.object(cron_health, "get_scheduler",
                                return_value=FakeBackend([])):
            rc = cron_health.main(["check"])
        self.assertEqual(rc, cron_health.EXIT_CONFIG_ERROR)

    def test_unsupported_platform_exits_two(self):
        def raise_unsupported():
            raise UnsupportedPlatformError("no backend for linux")
        with mock.patch.object(cron_health, "get_scheduler", raise_unsupported):
            rc = cron_health.main(["check"])
        self.assertEqual(rc, cron_health.EXIT_CONFIG_ERROR)

    def test_check_clean_exits_zero(self):
        backend = FakeBackend([ObservedEntry(
            entry_id="x", command=["python", "-m", "runner.run"],
            cwd=str(self.apiary),
            schedule={"type": "daily", "time": "02:00"},
        )])
        with mock.patch.object(cron_health, "get_scheduler",
                                return_value=backend):
            rc = cron_health.main(["check"])
        self.assertEqual(rc, cron_health.EXIT_OK)

    def test_check_drift_exits_one(self):
        backend = FakeBackend([])
        with mock.patch.object(cron_health, "get_scheduler",
                                return_value=backend):
            rc = cron_health.main(["check"])
        self.assertEqual(rc, cron_health.EXIT_DRIFT)

    def test_repair_without_apply_is_dry_run(self):
        backend = FakeBackend([])
        with mock.patch.object(cron_health, "get_scheduler",
                                return_value=backend):
            rc = cron_health.main(["repair"])
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual(backend.created, [])

    def test_repair_apply_mutates(self):
        backend = FakeBackend([])
        with mock.patch.object(cron_health, "get_scheduler",
                                return_value=backend):
            rc = cron_health.main(["repair", "--apply"])
        self.assertEqual(rc, cron_health.EXIT_OK)
        self.assertEqual(len(backend.created), 1)


class BootstrapHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.apiary = self.tmp / "apiary"
        self.apiary.mkdir()

    def tearDown(self):
        self._tmp.cleanup()




class GetSchedulerTests(unittest.TestCase):
    def test_non_windows_raises_unsupported(self):
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(UnsupportedPlatformError):
                cron_health.get_scheduler()

    def test_windows_without_schtasks_raises_unsupported(self):
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(UnsupportedPlatformError):
                    cron_health.get_scheduler()


if __name__ == "__main__":
    unittest.main()
