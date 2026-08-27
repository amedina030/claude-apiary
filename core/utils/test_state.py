"""Tests for the centralized state resolver in core/utils/state.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils import state


def _git_init(path: Path) -> None:
    """Initialize a fresh git repo at *path* with a single empty commit so
    ``git rev-parse --show-toplevel`` resolves to it. Tests rely on the
    resolver detecting a real git repo, not a directory that happens to
    have a .git folder."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )


class StateResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = self.root / "apiary"
        self.apiary.mkdir()
        # Apiary repo needs to look real enough for resolve_apiary_repo
        # callers that pass it explicitly. We don't init git on it.

    def _make_target(self, name: str = "myrepo") -> Path:
        target = self.root / name
        target.mkdir()
        _git_init(target)
        return target

    # --- Happy paths --------------------------------------------------

    def test_first_call_creates_repos_dir_registry_and_pointer(self):
        target = self._make_target("foo")
        state_dir = state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)

        # State dir under .repos/<name>-<id>/
        self.assertTrue(state_dir.is_dir())
        self.assertEqual(state_dir.parent, self.apiary / ".repos")
        self.assertTrue(state_dir.name.startswith("foo-"))

        # Registry has the entry
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), 1)
        only_id, entry = next(iter(registry.items()))
        self.assertEqual(entry["name"], "foo")
        self.assertEqual(Path(entry["real_path"]), target.resolve())
        self.assertTrue(entry["verified_ok"])
        self.assertIn("registered_at", entry)
        self.assertIn("last_used", entry)
        self.assertEqual(state_dir.name, f"foo-{only_id}")

        # Pointer breadcrumb exists in target
        pointer = target / ".apiary" / "pointer"
        self.assertTrue(pointer.is_file())
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        self.assertEqual(payload["target_id"], state_dir.name)
        self.assertEqual(Path(payload["apiary_repo"]), self.apiary.resolve())

    def test_second_call_reuses_entry_and_updates_last_used(self):
        target = self._make_target("foo")
        first = state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        registry_path = self.apiary / ".repos" / "registry.json"
        first_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        first_id, first_entry = next(iter(first_registry.items()))
        first_last_used = first_entry["last_used"]

        # Sleep a touch so the timestamp can move forward
        import time
        time.sleep(1.1)

        second = state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        self.assertEqual(first, second)

        second_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(len(second_registry), 1)  # no new entry
        self.assertNotEqual(second_registry[first_id]["last_used"], first_last_used)

    def test_two_repos_with_same_basename_get_distinct_ids(self):
        a = self.root / "parent_a" / "foo"
        a.mkdir(parents=True)
        _git_init(a)
        b = self.root / "parent_b" / "foo"
        b.mkdir(parents=True)
        _git_init(b)

        sa = state.resolve_target_state_dir(cwd=a, apiary_repo=self.apiary)
        sb = state.resolve_target_state_dir(cwd=b, apiary_repo=self.apiary)

        self.assertNotEqual(sa, sb)
        self.assertTrue(sa.name.startswith("foo-"))
        self.assertTrue(sb.name.startswith("foo-"))
        # Registry has two distinct entries pointing at distinct paths
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), 2)
        paths = {Path(e["real_path"]) for e in registry.values()}
        self.assertEqual(paths, {a.resolve(), b.resolve()})

    def test_id_counter_is_monotonic_and_never_reused(self):
        a = self._make_target("a_repo")
        b = self._make_target("b_repo")
        sa = state.resolve_target_state_dir(cwd=a, apiary_repo=self.apiary)
        sb = state.resolve_target_state_dir(cwd=b, apiary_repo=self.apiary)
        # Names end in -1, -2 in registration order
        self.assertEqual(sa.name, "a_repo-1")
        self.assertEqual(sb.name, "b_repo-2")
        # next_id file holds 2 (last allocated)
        next_id = (self.apiary / ".repos" / "next_id").read_text(encoding="utf-8").strip()
        self.assertEqual(next_id, "2")

    def test_resolves_when_invoked_from_subdirectory_of_target(self):
        target = self._make_target("foo")
        sub = target / "src" / "deeply" / "nested"
        sub.mkdir(parents=True)
        state_dir = state.resolve_target_state_dir(cwd=sub, apiary_repo=self.apiary)
        self.assertTrue(state_dir.name.startswith("foo-"))

    # --- Error paths --------------------------------------------------

    def test_not_inside_git_repo_raises(self):
        plain = self.root / "no_git_here"
        plain.mkdir()
        with self.assertRaises(RuntimeError) as ctx:
            state.resolve_target_state_dir(cwd=plain, apiary_repo=self.apiary)
        self.assertIn("Not inside a git repository", str(ctx.exception))

    def test_auto_register_disabled_raises_for_unknown_path(self):
        target = self._make_target("foo")
        with self.assertRaises(RuntimeError) as ctx:
            state.resolve_target_state_dir(
                cwd=target, apiary_repo=self.apiary, auto_register=False,
            )
        self.assertIn("not registered", str(ctx.exception).lower())

    # --- Edge cases ---------------------------------------------------

    def test_pointer_with_unknown_target_id_is_overwritten(self):
        target = self._make_target("foo")
        # Pre-write a stale pointer naming an id that doesn't exist yet.
        pointer_dir = target / ".apiary"
        pointer_dir.mkdir()
        (pointer_dir / "pointer").write_text(
            json.dumps({"apiary_repo": "/old", "target_id": "ghost-99"}),
            encoding="utf-8",
        )
        state_dir = state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        # Pointer was rewritten with the freshly assigned id
        new_payload = json.loads((pointer_dir / "pointer").read_text(encoding="utf-8"))
        self.assertEqual(new_payload["target_id"], state_dir.name)
        self.assertNotEqual(new_payload["target_id"], "ghost-99")

    def test_new_entry_includes_uid_and_version(self):
        # Phase 0: every new registry entry carries uid (int) and version
        # (apiary version) fields for the per-repo migration model.
        target = self._make_target("foo")
        # Pin a known apiary version so the assertion is deterministic.
        (self.apiary / "VERSION").write_text("0.1.0\n", encoding="utf-8")

        state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        only_id, entry = next(iter(registry.items()))
        self.assertEqual(entry["uid"], int(only_id))
        self.assertEqual(entry["version"], "0.1.0")

    def test_new_entry_uses_default_version_when_VERSION_missing(self):
        # During phase 0 a missing/empty VERSION falls back to the default.
        # No VERSION file in self.apiary at this point.
        target = self._make_target("foo")
        state.resolve_target_state_dir(cwd=target, apiary_repo=self.apiary)
        registry = json.loads((self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8"))
        _, entry = next(iter(registry.items()))
        self.assertEqual(entry["version"], state.DEFAULT_APIARY_VERSION)

    def test_unsafe_basename_is_sanitized(self):
        # Git won't accept truly nasty paths, but a name with spaces/dots
        # is plausible. We can't easily fabricate one git accepts on every
        # OS, so probe the helper directly.
        self.assertEqual(state._safe_name("my repo!"), "my-repo")
        self.assertEqual(state._safe_name("..."), "repo")
        self.assertEqual(state._safe_name(""), "repo")
        self.assertEqual(state._safe_name("ok-name_v2"), "ok-name_v2")


class ReserveUidTests(unittest.TestCase):
    """``reserve_uid`` keeps the monotonic contract when a uid is re-adopted.

    ``apiary install`` re-adopts the uid in a repo's self-pointer when the
    registry entry has been lost (Bug 4). The counter is usually lost with it,
    so without raising it the next allocation would hand the same uid to a
    different repo — two repos, one state dir.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apiary = Path(self._tmp.name)

    def test_raises_a_lost_counter_above_the_reserved_uid(self):
        state.reserve_uid(self.apiary, 7)
        self.assertEqual(state.allocate_next_id(self.apiary), 8)

    def test_never_lowers_the_counter(self):
        for _ in range(9):
            state.allocate_next_id(self.apiary)
        state.reserve_uid(self.apiary, 2)
        self.assertEqual(state.allocate_next_id(self.apiary), 10)


class EnvHelperTests(unittest.TestCase):
    def test_state_dir_from_env_returns_path_when_set(self):
        os.environ[state.TARGET_STATE_DIR_ENV] = "/some/path"
        try:
            self.assertEqual(state.state_dir_from_env(), Path("/some/path"))
        finally:
            del os.environ[state.TARGET_STATE_DIR_ENV]

    def test_state_dir_from_env_returns_none_when_unset(self):
        os.environ.pop(state.TARGET_STATE_DIR_ENV, None)
        self.assertIsNone(state.state_dir_from_env())

    def test_state_dir_from_env_returns_none_for_blank(self):
        os.environ[state.TARGET_STATE_DIR_ENV] = "   "
        try:
            self.assertIsNone(state.state_dir_from_env())
        finally:
            del os.environ[state.TARGET_STATE_DIR_ENV]


class PinModelHelperTests(unittest.TestCase):
    """Round-trip tests for the per-repo pin-model files (self-pointer,
    main-apiary-pointer, version) introduced by the per-repo migration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_pin_dir_is_under_dot_claude_apiary(self):
        self.assertEqual(state.pin_dir(self.repo), self.repo / ".claude" / "apiary")

    def test_self_pointer_round_trip_injects_schema_version(self):
        payload = {"uid": 7, "name": "HexWorld", "real_path": str(self.repo)}
        path = state.write_self_pointer(self.repo, payload)
        self.assertTrue(path.is_file())
        loaded = state.read_self_pointer(self.repo)
        self.assertEqual(loaded["uid"], 7)
        self.assertEqual(loaded["name"], "HexWorld")
        self.assertEqual(loaded["schema_version"], state.PIN_SCHEMA_VERSION)

    def test_main_apiary_pointer_round_trip(self):
        payload = {"main_apiary_path": "/abs/main", "main_apiary_uid": 1}
        state.write_main_apiary_pointer(self.repo, payload)
        loaded = state.read_main_apiary_pointer(self.repo)
        self.assertEqual(loaded["main_apiary_path"], "/abs/main")
        self.assertEqual(loaded["main_apiary_uid"], 1)
        self.assertEqual(loaded["schema_version"], state.PIN_SCHEMA_VERSION)

    def test_version_round_trip(self):
        state.write_version(self.repo, {"apiary_version": "0.1.0", "pinned_at": "2026-05-05T00:00:00Z"})
        loaded = state.read_version(self.repo)
        self.assertEqual(loaded["apiary_version"], "0.1.0")
        self.assertEqual(loaded["schema_version"], state.PIN_SCHEMA_VERSION)

    def test_read_returns_none_when_missing(self):
        self.assertIsNone(state.read_self_pointer(self.repo))
        self.assertIsNone(state.read_main_apiary_pointer(self.repo))
        self.assertIsNone(state.read_version(self.repo))

    def test_read_returns_none_when_malformed(self):
        state.self_pointer_path(self.repo).parent.mkdir(parents=True, exist_ok=True)
        state.self_pointer_path(self.repo).write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(state.read_self_pointer(self.repo))

    def test_write_creates_parent_dir(self):
        # pin_dir does not exist at start
        self.assertFalse(state.pin_dir(self.repo).is_dir())
        state.write_self_pointer(self.repo, {"uid": 1, "name": "x", "real_path": "/p"})
        self.assertTrue(state.pin_dir(self.repo).is_dir())

    def test_write_is_atomic_no_tmp_left_behind(self):
        state.write_self_pointer(self.repo, {"uid": 1, "name": "x", "real_path": "/p"})
        leftover = list(state.pin_dir(self.repo).glob("*.tmp"))
        self.assertEqual(leftover, [])

    def test_caller_supplied_schema_version_is_preserved(self):
        # If a future migration writes schema_version=2, the helper should
        # not silently downgrade it. The {schema_version: 1, **payload}
        # spread lets the caller's value win.
        state.write_self_pointer(self.repo, {"schema_version": 2, "uid": 1, "name": "x", "real_path": "/p"})
        loaded = state.read_self_pointer(self.repo)
        self.assertEqual(loaded["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
