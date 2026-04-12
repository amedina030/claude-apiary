#!/usr/bin/env python3
"""Tests for runner/usher.py — ticket sizing gate."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner.usher import (
    _extract_file_paths,
    _extract_subsystems,
    score,
    assess,
    main,
)


class TestExtractFilePaths(unittest.TestCase):

    def test_structured_scope(self):
        scope = "Modified: runner/run.py, runner/executor.py. New: scripts/migrate.py"
        paths = _extract_file_paths(scope)
        self.assertEqual(paths, ["runner/run.py", "runner/executor.py", "scripts/migrate.py"])

    def test_nested_paths(self):
        scope = "core/utils/filelock.py"
        paths = _extract_file_paths(scope)
        self.assertEqual(paths, ["core/utils/filelock.py"])

    def test_empty_scope(self):
        self.assertEqual(_extract_file_paths(""), [])

    def test_no_paths_in_prose(self):
        scope = "runner orchestrator only"
        self.assertEqual(_extract_file_paths(scope), [])

    def test_dedup(self):
        scope = "runner/run.py, runner/run.py, runner/executor.py"
        paths = _extract_file_paths(scope)
        self.assertEqual(paths, ["runner/run.py", "runner/executor.py"])

    def test_multiple_extensions(self):
        scope = "runner/config.json, runner/usher.py, docs/reference/cli-tools.md"
        paths = _extract_file_paths(scope)
        self.assertEqual(len(paths), 3)


class TestExtractSubsystems(unittest.TestCase):

    def test_basic(self):
        paths = ["runner/run.py", "runner/executor.py", "core/startup.py"]
        self.assertEqual(_extract_subsystems(paths), {"runner", "core"})

    def test_deeply_nested(self):
        paths = ["core/utils/filelock.py"]
        self.assertEqual(_extract_subsystems(paths), {"core"})

    def test_empty(self):
        self.assertEqual(_extract_subsystems([]), set())

    def test_single_component_excluded(self):
        """Bare filenames like 'setup.py' have no parent dir — not a subsystem."""
        paths = ["setup.py"]
        self.assertEqual(_extract_subsystems(paths), set())


class TestScore(unittest.TestCase):

    def test_basic_score(self):
        data = {
            "scope": "Modified: runner/run.py, core/startup.py. New: scripts/migrate.py",
            "description": "A short description for testing purposes.",
        }
        result = score(data)
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(result["subsystem_count"], 3)
        self.assertEqual(result["description_chars"], len("A short description for testing purposes."))

    def test_missing_scope(self):
        result = score({"description": "something"})
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["subsystem_count"], 0)

    def test_missing_description(self):
        result = score({"scope": "runner/run.py"})
        self.assertEqual(result["description_chars"], 0)


class TestAssess(unittest.TestCase):

    def _make_config(self, max_files_pass=5, max_files_warn=8,
                     max_sub_pass=2, max_sub_warn=3,
                     max_desc_pass=2000, max_desc_warn=4000):
        return {
            "max_files": {"pass": max_files_pass, "warn": max_files_warn},
            "max_subsystems": {"pass": max_sub_pass, "warn": max_sub_warn},
            "max_description_chars": {"pass": max_desc_pass, "warn": max_desc_warn},
        }

    def test_pass_verdict(self):
        data = {
            "scope": "runner/run.py, runner/config.json",
            "description": "Short.",
        }
        verdict, metrics = assess(data, config=self._make_config())
        self.assertEqual(verdict, "pass")

    def test_warn_verdict(self):
        # 6 files (> pass=5, <= warn=8) across 2 subsystems
        files = ", ".join(f"runner/f{i}.py" for i in range(6))
        data = {"scope": files, "description": "Short."}
        verdict, metrics = assess(data, config=self._make_config())
        self.assertEqual(verdict, "warn")

    def test_fail_verdict_files(self):
        # 10 files (> warn=8)
        files = ", ".join(f"runner/f{i}.py" for i in range(10))
        data = {"scope": files, "description": "Short."}
        verdict, metrics = assess(data, config=self._make_config())
        self.assertEqual(verdict, "fail")

    def test_fail_verdict_subsystems(self):
        # 4 subsystems (> warn=3)
        data = {
            "scope": "runner/a.py, core/b.py, scripts/c.py, docs/d.md",
            "description": "Short.",
        }
        verdict, metrics = assess(data, config=self._make_config())
        self.assertEqual(verdict, "fail")

    def test_fail_verdict_description(self):
        data = {
            "scope": "runner/a.py",
            "description": "x" * 5000,
        }
        verdict, metrics = assess(data, config=self._make_config())
        self.assertEqual(verdict, "fail")

    def test_worst_wins(self):
        """If files=warn and subsystems=fail, overall is fail."""
        files = ", ".join(f"a/f{i}.py" for i in range(6))  # warn
        data = {
            "scope": f"{files}, b/g.py, c/h.py, d/i.py, e/j.py",  # 5 subsystems -> fail
            "description": "Short.",
        }
        verdict, _ = assess(data, config=self._make_config())
        self.assertEqual(verdict, "fail")

    def test_custom_config(self):
        data = {
            "scope": "runner/a.py, runner/b.py, runner/c.py",
            "description": "Short.",
        }
        # Strict config: pass=1
        verdict, _ = assess(data, config=self._make_config(max_files_pass=1, max_files_warn=2))
        self.assertEqual(verdict, "fail")

    def test_empty_scope_passes(self):
        data = {"scope": "", "description": "Short."}
        verdict, _ = assess(data, config=self._make_config())
        self.assertEqual(verdict, "pass")


class TestMain(unittest.TestCase):

    def _write_intake(self, tmp_dir, data):
        path = Path(tmp_dir) / "intake.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_cli_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intake(tmp, {
                "scope": "runner/a.py",
                "description": "Short.",
            })
            with mock.patch("sys.argv", ["usher", path]):
                with mock.patch("sys.exit") as mock_exit:
                    main()
                    mock_exit.assert_not_called()

    def test_cli_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = ", ".join(f"runner/f{i}.py" for i in range(10))
            path = self._write_intake(tmp, {
                "scope": files,
                "description": "Short.",
            })
            with mock.patch("sys.argv", ["usher", path]):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)

    def test_cli_warn_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = ", ".join(f"runner/f{i}.py" for i in range(10))
            path = self._write_intake(tmp, {
                "scope": files,
                "description": "Short.",
            })
            with mock.patch("sys.argv", ["usher", path, "--warn-only"]):
                with mock.patch("sys.exit") as mock_exit:
                    main()
                    mock_exit.assert_not_called()

    def test_cli_missing_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intake(tmp, {"description": "no scope here"})
            with mock.patch("sys.argv", ["usher", path]):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)

    def test_cli_file_not_found(self):
        with mock.patch("sys.argv", ["usher", "/nonexistent/file.json"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
