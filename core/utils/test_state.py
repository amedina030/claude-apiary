"""Tests for the centralized state resolver in core/utils/state.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class MainApiaryUidTests(unittest.TestCase):
    """One constant, not a literal ``1`` in drift/cascade/install."""

    def test_uid_is_one(self):
        self.assertEqual(state.MAIN_APIARY_UID, 1)

    def test_drift_and_cascade_read_the_shared_constant(self):
        from core import cascade, drift
        self.assertIs(drift.state.MAIN_APIARY_UID, state.MAIN_APIARY_UID)
        self.assertIs(cascade.state.MAIN_APIARY_UID, state.MAIN_APIARY_UID)


class ResolveStateDirTests(unittest.TestCase):
    """The one state-dir resolver: env -> pins -> breadcrumb -> in-repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.apiary = self.root / "apiary"
        (self.apiary / ".repos").mkdir(parents=True)
        self.repo = self.root / "target"
        self.repo.mkdir()
        _git_init(self.repo)
        # Every test starts with the launcher env var absent.
        self._env = mock.patch.dict(
            os.environ, {state.TARGET_STATE_DIR_ENV: ""}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(state.TARGET_STATE_DIR_ENV, None)

    def _register(self, name: str = "target", uid: int = 7) -> Path:
        """Give the repo real pins and create the state dir they name."""
        state_dir = self.apiary / ".repos" / f"{name}-{uid}"
        state_dir.mkdir(parents=True, exist_ok=True)
        state.write_main_apiary_pointer(self.repo, {
            "main_apiary_path": str(self.apiary),
            "main_apiary_uid": state.MAIN_APIARY_UID,
        })
        state.write_self_pointer(self.repo, {
            "uid": uid, "name": name, "real_path": str(self.repo)})
        return state_dir

    # --- precedence ---------------------------------------------------

    def test_env_var_wins_over_everything(self):
        self._register()
        env_dir = self.root / "from-env"
        with mock.patch.dict(os.environ, {state.TARGET_STATE_DIR_ENV: str(env_dir)}):
            self.assertEqual(
                state.resolve_state_dir(self.repo, subdir="scribe"),
                env_dir / "scribe",
            )

    def test_use_env_false_ignores_the_launcher_variable(self):
        expected = self._register()
        env_dir = self.root / "from-env"
        with mock.patch.dict(os.environ, {state.TARGET_STATE_DIR_ENV: str(env_dir)}):
            self.assertEqual(
                state.resolve_state_dir(self.repo, subdir="scribe", use_env=False),
                expected / "scribe",
            )

    def test_pins_resolve_to_the_centralized_state_dir(self):
        expected = self._register()
        self.assertEqual(
            state.resolve_state_dir(self.repo, subdir="compass"),
            expected / "compass",
        )

    def test_pins_are_ignored_when_the_state_dir_does_not_exist(self):
        # Pin files can outlive the directory they name (main-apiary moved,
        # state pruned). Falling through beats returning a dead path.
        state.write_main_apiary_pointer(self.repo, {"main_apiary_path": str(self.apiary)})
        state.write_self_pointer(self.repo, {"uid": 99, "name": "gone", "real_path": str(self.repo)})
        self.assertEqual(
            state.resolve_state_dir(self.repo, subdir="scribe"),
            self.repo / ".apiary" / "scribe",
        )

    def test_legacy_breadcrumb_is_used_when_there_are_no_pins(self):
        legacy = self.apiary / ".repos" / "target-3"
        legacy.mkdir(parents=True)
        state._write_pointer(self.repo, self.apiary, "target-3")
        self.assertEqual(
            state.resolve_state_dir(self.repo, subdir="scribe"),
            legacy / "scribe",
        )

    def test_falls_back_to_the_in_repo_layout(self):
        self.assertEqual(
            state.resolve_state_dir(self.repo, subdir="research"),
            self.repo / ".apiary" / "research",
        )

    def test_legacy_in_repo_false_returns_none_instead(self):
        self.assertIsNone(
            state.resolve_state_dir(self.repo, subdir="sessions", legacy_in_repo=False)
        )

    # --- start vs repo, and the no-repo cases -------------------------

    def test_start_inside_the_repo_resolves_via_git(self):
        nested = self.repo / "deep" / "deeper"
        nested.mkdir(parents=True)
        self.assertEqual(
            state.resolve_state_dir(nested, subdir="scribe"),
            self.repo / ".apiary" / "scribe",
        )

    def test_repo_argument_skips_git_entirely(self):
        plain = self.root / "not-a-repo"
        plain.mkdir()
        with mock.patch("core.utils.state.git_root",
                        side_effect=AssertionError("git must not run")):
            self.assertEqual(
                state.resolve_state_dir(repo=plain, subdir="scribe"),
                plain / ".apiary" / "scribe",
            )

    def test_outside_a_git_repo_returns_none_by_default(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(state.resolve_state_dir(plain, subdir="scribe"))

    def test_cwd_fallback_serves_the_knowledge_stores_outside_a_repo(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(
            state.resolve_state_dir(plain, subdir="captures", cwd_fallback=True),
            plain / ".apiary" / "captures",
        )

    # --- require_exists -----------------------------------------------

    def test_require_exists_skips_a_subdir_that_is_not_there(self):
        self._register()
        self.assertIsNone(
            state.resolve_state_dir(repo=self.repo, subdir="scribe",
                                    use_env=False, require_exists=True)
        )

    def test_require_exists_returns_the_subdir_once_it_exists(self):
        state_dir = self._register()
        (state_dir / "scribe").mkdir()
        self.assertEqual(
            state.resolve_state_dir(repo=self.repo, subdir="scribe",
                                    use_env=False, require_exists=True),
            state_dir / "scribe",
        )

    def test_no_subdir_returns_the_state_dir_itself(self):
        expected = self._register()
        self.assertEqual(state.resolve_state_dir(self.repo), expected)

    def test_find_state_dir_agrees_with_the_pins_branch(self):
        expected = self._register()
        self.assertEqual(state.find_state_dir(self.repo), expected)


class ResolveApiaryRepoTests(unittest.TestCase):
    """``resolve_apiary_repo`` must not prefer the source tree it happens to
    be running from over the registered main repo — a worktree of
    main-apiary used to become "main-apiary" and grow its own ``.repos/``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.main = self.root / "main-apiary"
        (self.main / "core").mkdir(parents=True)
        (self.main / "core" / "install.py").write_text("", encoding="utf-8")
        (self.main / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        _git_init(self.main)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "add", "-A"], cwd=self.main, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "files"], cwd=self.main, check=True,
            capture_output=True)
        # Neutral cwd with no pin, and no launcher env var.
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.elsewhere)
        self.addCleanup(os.chdir, self._cwd)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("APIARY_MAIN_REPO", None)

    def _add_worktree(self) -> Path:
        wt = self.root / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(wt)],
            cwd=self.main, check=True, capture_output=True,
        )
        return wt

    def test_worktree_checkout_resolves_to_the_registered_main_repo(self):
        wt = self._add_worktree()
        self.assertTrue((wt / "core" / "install.py").is_file())  # looks like apiary
        with mock.patch.object(state, "_REPO_ROOT", wt):
            self.assertEqual(state.resolve_apiary_repo(), self.main)

    def test_main_checkout_still_resolves_to_itself(self):
        with mock.patch.object(state, "_REPO_ROOT", self.main):
            self.assertEqual(state.resolve_apiary_repo(), self.main)

    def test_pin_outranks_the_source_tree(self):
        # A relocated main-apiary: the source tree we run from is a stale
        # copy, the pin in the cwd names the live one.
        other = self.root / "live-apiary"
        (other / "core").mkdir(parents=True)
        (other / "core" / "install.py").write_text("", encoding="utf-8")
        (other / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        state.write_main_apiary_pointer(self.elsewhere, {
            "main_apiary_path": str(other),
            "main_apiary_uid": state.MAIN_APIARY_UID,
        })
        with mock.patch.object(state, "_REPO_ROOT", self.main):
            self.assertEqual(state.resolve_apiary_repo(), other)

    def test_explicit_argument_and_env_var_still_win(self):
        elsewhere = self.root / "explicit"
        elsewhere.mkdir()
        with mock.patch.object(state, "_REPO_ROOT", self.main):
            self.assertEqual(state.resolve_apiary_repo(elsewhere), elsewhere)
            with mock.patch.dict(os.environ, {"APIARY_MAIN_REPO": str(elsewhere)}):
                self.assertEqual(state.resolve_apiary_repo(), elsewhere)

    def test_raises_when_nothing_resolves(self):
        not_apiary = self.root / "random"
        not_apiary.mkdir()
        with mock.patch.object(state, "_REPO_ROOT", not_apiary):
            with self.assertRaises(RuntimeError):
                state.resolve_apiary_repo()


if __name__ == "__main__":
    unittest.main()
