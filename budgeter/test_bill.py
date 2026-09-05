#!/usr/bin/env python3
"""Tests for budgeter/bill.py against the transcript fixture in test_transcripts."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from budgeter import bill
from budgeter.test_transcripts import make_projects


def run_bill(*argv) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = bill.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class TestBillCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_projects(self.root)
        self.common = ["--projects-dir", str(self.root), "--since", "2026-09-01"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_json_by_project(self):
        rc, out, _ = run_bill(*self.common, "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["by"], "project")
        names = [r["name"] for r in data["rows"]]
        self.assertEqual(names[0], "alpha")  # largest load first
        self.assertIn("alpha (runner worktrees)", names)
        self.assertEqual(data["unweighted_models"], ["claude-mystery-9"])
        self.assertEqual(data["sessions"], 3)
        alpha = data["rows"][0]
        self.assertEqual(alpha["calls"], 3)
        self.assertGreater(alpha["interactive_load"], 0)

    def test_text_by_session_shows_label_kind_and_prompt(self):
        rc, out, _ = run_bill(*self.common, "--by", "session")
        self.assertEqual(rc, 0)
        self.assertIn("fix the thing please", out)
        self.assertIn("alpha (runner worktrees)", out)
        self.assertIn("headless", out)
        self.assertIn("not a bill", out)
        self.assertIn("unweighted models", out)

    def test_project_filter_and_top(self):
        rc, out, _ = run_bill(*self.common, "--project", "runner", "--json")
        data = json.loads(out)
        self.assertEqual([r["name"] for r in data["rows"]], ["alpha (runner worktrees)"])
        rc, out, _ = run_bill(*self.common, "--by", "session", "--top", "1")
        self.assertIn("more row(s)", out)

    def test_bad_since_is_a_clean_error(self):
        rc, out, err = run_bill("--projects-dir", str(self.root), "--since", "lately")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertEqual(out, "")

    def test_empty_root_renders_zero_rows(self):
        rc, out, _ = run_bill("--projects-dir", str(self.root / "empty"), "--since", "2026-09-01")
        self.assertEqual(rc, 0)
        self.assertIn("sessions 0", out)


if __name__ == "__main__":
    unittest.main()
