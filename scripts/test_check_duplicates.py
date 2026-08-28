"""Tests for ``scripts/check_duplicates.py`` — the AST near-duplicate check."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_duplicates as cd  # noqa: E402


def _write(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


BODY = """
    def {name}({arg}):
        {doc}
        {v1} = {arg}.strip()
        if not {v1}:
            return None
        {v2} = []
        for {v3} in {v1}.split(","):
            {v2}.append({v3}.upper())
        {v2}.sort()
        return {v2}
"""


def _fn(name="handle", arg="raw", v1="text", v2="out", v3="part", doc='"""Doc."""'):
    return BODY.format(name=name, arg=arg, v1=v1, v2=v2, v3=v3, doc=doc)


class TempTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _collect(self, min_statements: int = 5):
        functions, errors = cd.collect(self.root, min_statements)
        self.assertEqual(errors, [])
        return functions


class NormalizationTests(TempTreeCase):
    def test_renamed_locals_and_arguments_still_match(self):
        _write(self.root, "a.py", _fn())
        _write(
            self.root,
            "b.py",
            _fn(
                name="process",
                arg="value",
                v1="cleaned",
                v2="result",
                v3="chunk",
                doc='"""Completely different docstring."""',
            ),
        )
        groups = cd.identical_groups(self._collect())
        self.assertEqual(len(groups), 1)
        self.assertEqual({f.path.name for f in groups[0]}, {"a.py", "b.py"})

    def test_a_different_call_target_is_not_a_duplicate(self):
        _write(self.root, "a.py", _fn())
        # Same shape, but `.lower()` instead of `.upper()`.
        _write(self.root, "b.py", _fn().replace(".upper()", ".lower()"))
        self.assertEqual(cd.identical_groups(self._collect()), [])

    def test_methods_and_nested_functions_are_reached(self):
        # _fn() is already indented one level, so it drops straight into a
        # class body or another def.
        _write(self.root, "a.py", "class Holder:\n" + _fn())
        _write(self.root, "b.py", "def outer():\n" + _fn(name="inner") + "    return inner\n")
        names = {f.qualname for f in self._collect()}
        self.assertIn("Holder.handle", names)
        self.assertIn("outer.inner", names)

    def test_short_functions_are_ignored(self):
        _write(self.root, "a.py", "def f(x):\n    return x + 1\n")
        _write(self.root, "b.py", "def g(y):\n    return y + 1\n")
        self.assertEqual(self._collect(min_statements=5), [])
        self.assertEqual(len(self._collect(min_statements=1)), 2)


class OverlapTests(TempTreeCase):
    def test_a_near_copy_is_reported_as_a_pair(self):
        _write(self.root, "a.py", _fn())
        # Same function with one statement dropped — a copy that drifted.
        _write(self.root, "b.py", _fn(name="process").replace("        out.sort()\n", ""))
        functions = self._collect()
        pairs = cd.near_duplicate_pairs(functions, 0.7)
        self.assertEqual(len(pairs), 1)
        score, a, b = pairs[0]
        self.assertGreaterEqual(score, 0.7)
        self.assertLess(score, 1.0)
        self.assertEqual({a.path.name, b.path.name}, {"a.py", "b.py"})

    def test_exact_duplicates_are_not_double_reported_as_pairs(self):
        _write(self.root, "a.py", _fn())
        _write(self.root, "b.py", _fn(name="process"))
        functions = self._collect()
        self.assertEqual(len(cd.identical_groups(functions)), 1)
        self.assertEqual(cd.near_duplicate_pairs(functions, 0.7), [])

    def test_unrelated_functions_score_below_the_threshold(self):
        _write(self.root, "a.py", _fn())
        _write(
            self.root,
            "b.py",
            """
        def unrelated(path):
            data = {}
            with open(path) as fh:
                for line in fh:
                    key, _, value = line.partition("=")
                    data[key] = value
            return data
        """,
        )
        self.assertEqual(cd.near_duplicate_pairs(self._collect(), 0.85), [])

    def test_overlap_of_a_body_with_itself_is_one(self):
        _write(self.root, "a.py", _fn())
        func = self._collect()[0]
        self.assertEqual(cd.overlap(func, func), 1.0)


class FileWalkTests(TempTreeCase):
    def test_skipped_directories_are_not_scanned(self):
        _write(self.root, "keep/a.py", _fn())
        for skipped in (".venv", "__pycache__", ".repos", "node_modules"):
            _write(self.root, f"{skipped}/b.py", _fn())
        found = {p.name for p in cd.iter_python_files(self.root)}
        self.assertEqual(found, {"a.py"})

    def test_a_skipped_name_above_the_root_does_not_exclude_everything(self):
        # An agent worktree lives under .claude/worktrees/<name>/ — scanning it
        # must still find its files (the bug this check shipped with).
        nested = self.root / ".claude" / "worktrees" / "wt"
        _write(nested, "a.py", _fn())
        self.assertEqual([p.name for p in cd.iter_python_files(nested)], ["a.py"])

    def test_a_single_file_path_is_accepted(self):
        path = _write(self.root, "a.py", _fn())
        self.assertEqual(cd.iter_python_files(path), [path])

    def test_an_unparseable_file_is_reported_not_fatal(self):
        _write(self.root, "a.py", _fn())
        _write(self.root, "broken.py", "def (:\n")
        functions, errors = cd.collect(self.root, 5)
        self.assertEqual(len(functions), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("broken.py", errors[0])


class CliTests(TempTreeCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cd.main(argv)
        return rc, buf.getvalue() + err.getvalue()

    def test_report_only_by_default(self):
        _write(self.root, "a.py", _fn())
        _write(self.root, "b.py", _fn(name="process"))
        rc, out = self._run(["--path", str(self.root), "--min-statements", "5"])
        self.assertEqual(rc, 0)
        self.assertIn("Identical function bodies", out)
        self.assertIn("a.py", out)

    def test_fail_on_identical_exits_1(self):
        _write(self.root, "a.py", _fn())
        _write(self.root, "b.py", _fn(name="process"))
        rc, _ = self._run(
            [
                "--path",
                str(self.root),
                "--min-statements",
                "5",
                "--fail-on-identical",
            ]
        )
        self.assertEqual(rc, 1)

    def test_fail_on_identical_still_exits_0_when_clean(self):
        _write(self.root, "a.py", _fn())
        rc, _ = self._run(
            [
                "--path",
                str(self.root),
                "--min-statements",
                "5",
                "--fail-on-identical",
            ]
        )
        self.assertEqual(rc, 0)

    def test_quiet_prints_one_line(self):
        _write(self.root, "a.py", _fn())
        rc, out = self._run(["--path", str(self.root), "--quiet"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_missing_path_exits_2(self):
        rc, out = self._run(["--path", str(self.root / "nope")])
        self.assertEqual(rc, 2)
        self.assertIn("no such path", out)

    def test_a_threshold_outside_the_range_exits_2(self):
        for bad in ("0", "1.5", "-0.2"):
            with self.subTest(bad=bad):
                rc, _ = self._run(["--path", str(self.root), "--threshold", bad])
                self.assertEqual(rc, 2)

    def test_min_statements_below_one_exits_2(self):
        rc, _ = self._run(["--path", str(self.root), "--min-statements", "0"])
        self.assertEqual(rc, 2)


class RepoScanTests(unittest.TestCase):
    def test_the_real_tree_scans_without_a_parse_error(self):
        """A syntax error anywhere in the repo would show up here first."""
        functions, errors = cd.collect(REPO_ROOT, cd.DEFAULT_MIN_STATEMENTS)
        self.assertEqual(errors, [])
        self.assertGreater(len(functions), 100)


if __name__ == "__main__":
    unittest.main()
