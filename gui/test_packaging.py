"""Tests for the PyInstaller spec's build-time logic.

`gui/packaging/*` had no tests, and its failure mode is expensive: a mistake
surfaces minutes into a build, or — worse — as a bundle that is missing a file
and only breaks when the exe runs on another machine.

A spec is a Python script PyInstaller `exec`s with `Analysis`/`PYZ`/`EXE`/
`COLLECT` and `SPEC`/`workpath`/`distpath` injected into its globals. So we
exec it here with those stubbed and inspect what it asked for — no PyInstaller
install and no actual build required.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gui import build_info

SPEC_PATH = Path(__file__).resolve().parent / "packaging" / "apiary_gui.spec"
REPO_ROOT = Path(__file__).resolve().parent.parent


class _Recorder:
    """Stands in for Analysis/PYZ/EXE/COLLECT; records the kwargs it got."""

    def __init__(self, calls: dict, name: str):
        self._calls = calls
        self._name = name

    def __call__(self, *args, **kwargs):
        self._calls[self._name] = {"args": args, "kwargs": kwargs}
        return self

    # Analysis exposes .pure/.scripts/.binaries/.datas to the rest of the spec.
    def __getattr__(self, item):
        return self


def run_spec(workpath: Path) -> dict:
    """Exec the spec with PyInstaller's injected globals stubbed out."""
    calls: dict = {}
    globals_: dict = {
        "__file__": str(SPEC_PATH),
        "SPEC": str(SPEC_PATH),
        "workpath": str(workpath),
        "distpath": str(workpath / "dist"),
    }
    for name in ("Analysis", "PYZ", "EXE", "COLLECT"):
        globals_[name] = _Recorder(calls, name)
    exec(compile(SPEC_PATH.read_text(encoding="utf-8"), str(SPEC_PATH), "exec"), globals_)
    return {"calls": calls, "globals": globals_}


class SpecTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workpath = Path(self._tmp.name).resolve()
        self.result = run_spec(self.workpath)
        self.analysis = self.result["calls"]["Analysis"]["kwargs"]

    def test_the_spec_stamps_the_build_into_the_workpath(self):
        stamp = self.workpath / build_info.BUILD_INFO_NAME
        self.assertTrue(stamp.is_file(), "the spec must write build_info.json")
        info = json.loads(stamp.read_text(encoding="utf-8"))
        self.assertEqual(info["version"], build_info.BASE_VERSION)
        self.assertIn("commit", info)
        self.assertIn("built_at", info)

    def test_the_stamp_is_bundled_where_the_runtime_looks_for_it(self):
        # gui/build_info.py reads <_MEIPASS>/gui/build_info.json, so the data
        # entry has to land in "gui" — not "gui/web", not the bundle root.
        targets = {Path(src).name: dest for src, dest in self.analysis["datas"]}
        self.assertEqual(targets.get(build_info.BUILD_INFO_NAME), "gui")

    def test_the_frontend_is_bundled_under_gui_web(self):
        dests = {dest for _src, dest in self.analysis["datas"]}
        self.assertIn("gui/web", dests)

    def test_the_entry_point_is_the_gui_app(self):
        scripts = self.result["calls"]["Analysis"]["args"][0]
        self.assertEqual([Path(s).name for s in scripts], ["app.py"])

    def test_windows_only_backends_are_excluded(self):
        for excluded in ("webview.platforms.cocoa", "webview.platforms.gtk"):
            self.assertIn(excluded, self.analysis["excludes"])

    def test_the_bundle_is_windowed_and_named_apiary_gui(self):
        exe = self.result["calls"]["EXE"]["kwargs"]
        self.assertEqual(exe["name"], "apiary-gui")
        self.assertFalse(exe["console"], "a GUI build must not open a console")
        self.assertEqual(self.result["calls"]["COLLECT"]["kwargs"]["name"], "apiary-gui")


class BuildScriptTest(unittest.TestCase):
    def test_pyinstaller_is_pinned_in_the_build_group(self):
        # The one tool that decides what ends up in the .exe must be pinned and
        # in the lockfile, not `pip install`-ed ad hoc (review gui, build repro).
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.poetry.group.build.dependencies]", text)
        self.assertIn("pyinstaller", text)

    def test_the_build_script_looks_for_the_stamp_where_the_spec_puts_it(self):
        from gui.packaging import build as build_script  # noqa: PLC0415

        self.assertEqual(
            build_script.BUILD_INFO_REL.as_posix(),
            f"_internal/gui/{build_info.BUILD_INFO_NAME}",
        )


if __name__ == "__main__":
    unittest.main()
