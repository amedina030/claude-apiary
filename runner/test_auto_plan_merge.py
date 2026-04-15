#!/usr/bin/env python3
"""Tests for _merge_subsumed_steps in runner/auto_plan.py (T-2026-127)."""
import unittest

from runner.auto_plan import _merge_subsumed_steps


def _step(num, action, files, depends_on=None, code_spec="spec", description="d",
          post_conditions=None):
    s = {
        "step_number": num,
        "type": action,
        "action": action,
        "description": description,
        "files": files,
        "depends_on": depends_on if depends_on is not None else [],
        "code_spec": code_spec,
    }
    if post_conditions is not None:
        s["post_conditions"] = post_conditions
    return s


class MergeSubsumedStepsTests(unittest.TestCase):
    def test_merges_consecutive_same_file_chain(self):
        steps = [
            _step(1, "create", ["a.py"], code_spec="add fn X",
                  post_conditions=[{"type": "file_contains", "file": "a.py", "text": "def X"}]),
            _step(2, "modify", ["a.py"], depends_on=[1], code_spec="call X from Y",
                  post_conditions=[{"type": "file_contains", "file": "a.py", "text": "X()"}]),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["step_number"], 1)
        self.assertEqual(out[0]["action"], "create")
        self.assertIn("add fn X", out[0]["code_spec"])
        self.assertIn("call X from Y", out[0]["code_spec"])
        self.assertEqual(len(out[0]["post_conditions"]), 2)

    def test_does_not_merge_when_b_files_not_subset(self):
        steps = [
            _step(1, "create", ["a.py"]),
            _step(2, "modify", ["a.py", "b.py"], depends_on=[1]),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 2)

    def test_does_not_merge_when_extra_dependency(self):
        steps = [
            _step(1, "create", ["a.py"]),
            _step(2, "create", ["b.py"]),
            _step(3, "modify", ["a.py"], depends_on=[1, 2]),
        ]
        out = _merge_subsumed_steps(steps)
        # step 3 depends on both 1 and 2, so it isn't solely subsumed by 1
        self.assertEqual(len(out), 3)

    def test_does_not_merge_test_or_verify(self):
        steps = [
            _step(1, "create", ["a.py"]),
            _step(2, "test", ["a.py"], depends_on=[1],
                  code_spec="python -m unittest runner.test_a"),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["action"], "test")

    def test_iterative_three_step_collapse(self):
        steps = [
            _step(1, "create", ["a.py"], code_spec="S1"),
            _step(2, "modify", ["a.py"], depends_on=[1], code_spec="S2"),
            _step(3, "modify", ["a.py"], depends_on=[2], code_spec="S3"),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 1)
        for tag in ("S1", "S2", "S3"):
            self.assertIn(tag, out[0]["code_spec"])

    def test_renumbers_and_remaps_downstream_deps(self):
        steps = [
            _step(1, "create", ["a.py"]),
            _step(2, "modify", ["a.py"], depends_on=[1]),  # merges into 1
            _step(3, "create", ["b.py"], depends_on=[2]),  # dep should remap to 1
            _step(4, "test", ["b.py"], depends_on=[3],
                  code_spec="python -m unittest runner.test_b"),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual([s["step_number"] for s in out], [1, 2, 3])
        self.assertEqual(out[1]["depends_on"], [1])
        self.assertEqual(out[2]["depends_on"], [2])

    def test_path_normalization_backslash_vs_forward(self):
        steps = [
            _step(1, "create", ["dir/a.py"]),
            _step(2, "modify", ["dir\\a.py"], depends_on=[1]),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 1)

    def test_empty_list_returns_empty(self):
        self.assertEqual(_merge_subsumed_steps([]), [])

    def test_non_list_passes_through(self):
        self.assertEqual(_merge_subsumed_steps("nope"), "nope")

    def test_no_merge_when_depends_on_missing_predecessor(self):
        steps = [
            _step(1, "create", ["a.py"]),
            _step(2, "modify", ["a.py"], depends_on=[]),
        ]
        out = _merge_subsumed_steps(steps)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
