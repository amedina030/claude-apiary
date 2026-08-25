"""Parity tests for the two secret-scanning gates (#T-2026-260).

The commit-time gate (``scripts/secret_scan.py``) and the push-time gate
(``core/hooks/pre_push_secret_scan.py``) once carried independent regex tables.
Nothing failed when they drifted, so a pattern added to one silently left the
other weaker. They now share ``core/secret_patterns``; these tests are what
keep that true.

Fixtures are deliberately fake credentials — syntactically valid for their
rule, corresponding to nothing real. ``.secretsallow`` exempts this file.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import secret_patterns  # noqa: E402
from core.hooks import pre_push_secret_scan as push_gate  # noqa: E402
from scripts import secret_scan as commit_gate  # noqa: E402

# One fixture per shared rule. Assembled from parts so the literals are not
# themselves grep-able as credentials.
FIXTURES = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----",
    "aws-access-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "anthropic-key": "sk-ant-" + "a" * 32,
    "openai-key": "sk-" + "B" * 32,
    "github-token": "ghp_" + "c" * 36,
    "slack-token": "xoxb-" + "1234567890-abcdefghij",
    "google-api-key": "AIza" + "D" * 35,
    "bearer-token": "Authorization: Bearer " + "e" * 24,
    "basic-auth-url": "https://user:hunter2pass@example.com/x",
}


class SharedTableTests(unittest.TestCase):
    def test_every_shared_rule_has_a_fixture(self):
        names = {p.name for p in secret_patterns.PATTERNS}
        self.assertEqual(
            names,
            set(FIXTURES),
            "a rule was added to core/secret_patterns without a parity fixture",
        )

    def test_rule_names_are_unique(self):
        names = [p.name for p in secret_patterns.PATTERNS]
        self.assertEqual(len(names), len(set(names)))

    def test_find_returns_the_first_matching_rule(self):
        for name, text in FIXTURES.items():
            with self.subTest(rule=name):
                hit = secret_patterns.find(text)
                self.assertIsNotNone(hit, f"{name} did not match its fixture")
                self.assertEqual(hit[0], name)

    def test_more_specific_rule_wins(self):
        # sk-ant- is also matchable by a naive sk- rule; the narrower label is
        # the useful one, so ordering in the table is load-bearing.
        hit = secret_patterns.find(FIXTURES["anthropic-key"])
        self.assertEqual(hit[0], "anthropic-key")


class GateParityTests(unittest.TestCase):
    """Both gates must flag the same fixture under the same name."""

    def test_commit_gate_flags_every_fixture(self):
        for name, text in FIXTURES.items():
            with self.subTest(rule=name):
                found = commit_gate.scan_lines([("f.py", 1, text)])
                self.assertEqual(len(found), 1, f"commit gate missed {name}")
                self.assertEqual(found[0].pattern, name)

    def test_push_gate_flags_every_fixture(self):
        for name, text in FIXTURES.items():
            with self.subTest(rule=name):
                hits = push_gate.scan_line(text)
                self.assertTrue(hits, f"push gate missed {name}")
                self.assertIn(name, [rule for rule, _ in hits])

    def test_neither_gate_flags_ordinary_code(self):
        clean = "def main():\n    return 0"
        self.assertEqual(commit_gate.scan_lines([("f.py", 1, clean)]), [])
        self.assertEqual(push_gate.scan_line(clean), [])


class PragmaParityTests(unittest.TestCase):
    """A line silenced for one gate must be silenced for the other.

    The push gate shipped with detect-secrets' spelling; the commit gate added
    its own. Two gates disagreeing about what is allowlisted would be worse
    than either alone — you would silence one and be blocked by the other with
    no obvious reason.
    """

    MARKERS = ("# apiary:allow-secret", "# pragma: allowlist secret")

    def test_both_gates_honour_both_markers(self):
        for marker in self.MARKERS:
            for name, text in FIXTURES.items():
                line = f'k = "{text}"  {marker}'
                with self.subTest(marker=marker, rule=name):
                    self.assertEqual(commit_gate.scan_lines([("f.py", 1, line)]), [])
                    self.assertEqual(push_gate.scan_line(line), [])

    def test_marker_is_case_insensitive(self):
        line = f'k = "{FIXTURES["aws-access-key"]}"  # PRAGMA: ALLOWLIST SECRET'
        self.assertEqual(commit_gate.scan_lines([("f.py", 1, line)]), [])
        self.assertEqual(push_gate.scan_line(line), [])

    def test_without_a_marker_both_still_fire(self):
        line = f'k = "{FIXTURES["aws-access-key"]}"'
        self.assertEqual(len(commit_gate.scan_lines([("f.py", 1, line)])), 1)
        self.assertTrue(push_gate.scan_line(line))


if __name__ == "__main__":
    unittest.main()
