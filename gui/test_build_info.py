"""Tests for gui.build_info — the commit stamp written into packaged builds.

The stamp exists so a bundle can be traced back to a tree state, which means
two properties matter more than the happy path: it must never claim a commit
the tree does not actually match (dirty must be reported), and it must never
raise — no git, no bundle, or a corrupt stamp all have to degrade to
"unknown" rather than fail a build or a launch.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui import build_info


class VersionStringTest(unittest.TestCase):
    def test_clean_commit_renders_as_a_pep440_local_version(self):
        info = {"version": "0.1.0", "commit": "1a2b3c4d5e6f", "dirty": False}
        self.assertEqual(build_info.version_string(info), "0.1.0+g1a2b3c4d5e6f")

    def test_a_dirty_tree_is_marked(self):
        info = {"version": "0.1.0", "commit": "1a2b3c4d5e6f", "dirty": True}
        self.assertEqual(build_info.version_string(info), "0.1.0+g1a2b3c4d5e6f.dirty")

    def test_missing_commit_reports_unknown(self):
        self.assertEqual(
            build_info.version_string({"version": "0.1.0", "commit": ""}),
            "0.1.0+unknown",
        )

    def test_empty_info_still_yields_a_string(self):
        self.assertEqual(
            build_info.version_string({}), f"{build_info.BASE_VERSION}+unknown"
        )


class GitStateTest(unittest.TestCase):
    def _with_git(self, results):
        """Patch _run_git with a canned {args-tuple: output} map."""
        def fake(args, cwd):
            return results.get(tuple(args))
        return mock.patch.object(build_info, "_run_git", side_effect=fake)

    def test_clean_checkout(self):
        rev = ("rev-parse", f"--short={build_info.COMMIT_LEN}", "HEAD")
        with self._with_git({rev: "abc123def456", ("status", "--porcelain", "--untracked-files=no"): ""}):
            self.assertEqual(build_info.git_state(Path(".")), ("abc123def456", False))

    def test_modified_tracked_files_report_dirty(self):
        rev = ("rev-parse", f"--short={build_info.COMMIT_LEN}", "HEAD")
        status = ("status", "--porcelain", "--untracked-files=no")
        with self._with_git({rev: "abc123def456", status: " M gui/app.py"}):
            self.assertEqual(build_info.git_state(Path(".")), ("abc123def456", True))

    def test_a_failed_status_call_is_assumed_dirty(self):
        # No evidence of cleanliness is not evidence of cleanliness: a stamp
        # that claims a commit it may not match is worse than one that doesn't.
        rev = ("rev-parse", f"--short={build_info.COMMIT_LEN}", "HEAD")
        with self._with_git({rev: "abc123def456"}):  # status -> None
            self.assertEqual(build_info.git_state(Path(".")), ("abc123def456", True))

    def test_no_git_at_all_is_unknown_not_an_error(self):
        with self._with_git({}):
            self.assertEqual(build_info.git_state(Path(".")), ("", False))

    def test_nonexistent_root_is_unknown(self):
        self.assertEqual(
            build_info.git_state(Path("no") / "such" / "dir"), ("", False)
        )

    def test_run_git_returns_none_when_git_is_absent(self):
        with mock.patch.object(build_info.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(build_info._run_git(["rev-parse", "HEAD"], Path(".")))

    def test_run_git_returns_none_on_nonzero_exit(self):
        completed = mock.Mock(returncode=128, stdout="")
        with mock.patch.object(build_info.subprocess, "run", return_value=completed):
            self.assertIsNone(build_info._run_git(["rev-parse", "HEAD"], Path(".")))


class WriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "work"

    def test_write_creates_a_readable_stamp_and_returns_it(self):
        info = {"version": "0.1.0", "commit": "deadbeefcafe", "dirty": False,
                "built_at": "2026-08-26T00:00:00Z"}
        path, written = build_info.write(self.dest, info=info)
        self.assertEqual(path.name, build_info.BUILD_INFO_NAME)
        self.assertEqual(written, info)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), info)

    def test_write_collects_when_no_info_is_supplied(self):
        with mock.patch.object(build_info, "collect", return_value={"commit": "x"}) as c:
            path, written = build_info.write(self.dest, root=Path("."))
            c.assert_called_once_with(Path("."))
        self.assertEqual(written, {"commit": "x"})
        self.assertTrue(path.is_file())

    def test_collect_carries_the_base_version_and_a_timestamp(self):
        with mock.patch.object(build_info, "git_state", return_value=("abc", True)):
            info = build_info.collect(Path("."))
        self.assertEqual(info["version"], build_info.BASE_VERSION)
        self.assertEqual(info["commit"], "abc")
        self.assertTrue(info["dirty"])
        self.assertTrue(info["built_at"].endswith("Z"))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # load() memoises; every test here needs a clean slate.
        patcher = mock.patch.object(build_info, "_cached", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bundle(self, payload: str) -> Path:
        p = self.dir / build_info.BUILD_INFO_NAME
        p.write_text(payload, encoding="utf-8")
        return p

    def test_a_bundled_stamp_wins_over_live_git(self):
        path = self._bundle(json.dumps({"version": "0.1.0", "commit": "bundled1234", "dirty": False}))
        with mock.patch.object(build_info, "bundled_path", return_value=path), \
             mock.patch.object(build_info, "collect") as collect:
            info = build_info.load(refresh=True)
        collect.assert_not_called()
        self.assertEqual(info["commit"], "bundled1234")
        self.assertEqual(info["origin"], "bundle")
        self.assertEqual(build_info.version_string(info), "0.1.0+gbundled1234")

    def test_a_corrupt_stamp_falls_back_to_git(self):
        path = self._bundle("{ not json")
        with mock.patch.object(build_info, "bundled_path", return_value=path), \
             mock.patch.object(build_info, "collect", return_value={"commit": "live12345678"}):
            info = build_info.load(refresh=True)
        self.assertEqual(info["commit"], "live12345678")
        self.assertEqual(info["origin"], "git")

    def test_no_bundle_and_no_git_is_unknown(self):
        with mock.patch.object(build_info, "bundled_path", return_value=None), \
             mock.patch.object(build_info, "collect", return_value={"commit": ""}):
            info = build_info.load(refresh=True)
        self.assertEqual(info["origin"], "unknown")
        self.assertEqual(build_info.version_string(info), "0.1.0+unknown")

    def test_load_is_cached_and_returns_a_copy(self):
        with mock.patch.object(build_info, "bundled_path", return_value=None), \
             mock.patch.object(build_info, "collect", return_value={"commit": "a"}) as collect:
            first = build_info.load(refresh=True)
            first["commit"] = "mutated"
            second = build_info.load()
            self.assertEqual(collect.call_count, 1, "the second call is served from cache")
        self.assertEqual(second["commit"], "a", "callers cannot corrupt the cache")

    def test_bundled_path_is_none_outside_a_frozen_build(self):
        self.assertIsNone(build_info.bundled_path())


class McpHandshakeTest(unittest.TestCase):
    """The MCP handshake reports the stamp instead of a frozen literal."""

    def test_initialize_reports_the_build_version(self):
        from gui import permission_mcp

        with mock.patch.object(build_info, "_cached", None), \
             mock.patch.object(build_info, "bundled_path", return_value=None), \
             mock.patch.object(build_info, "collect", return_value={"commit": "abc123abc123"}):
            result = permission_mcp.handle_initialize({})
        self.assertEqual(result["serverInfo"]["version"], "0.1.0+gabc123abc123")

    def test_a_broken_stamp_does_not_break_the_handshake(self):
        from gui import permission_mcp

        with mock.patch.object(build_info, "version_string", side_effect=RuntimeError("boom")):
            result = permission_mcp.handle_initialize({})
        self.assertEqual(result["serverInfo"]["version"], "0.1.0")


if __name__ == "__main__":
    unittest.main()
