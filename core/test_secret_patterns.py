"""Parity tests for the two secret-scanning gates (#T-2026-260).

The commit-time gate (``scripts/secret_scan.py``) and the push-time gate
(``core/hooks/pre_push_secret_scan.py``) once carried independent regex tables.
Nothing failed when they drifted, so a pattern added to one silently left the
other weaker. They now share ``core/secret_patterns`` — the literal table AND
the generic ``key = value`` rule; these tests are what keep that true.

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

# One fixture per shared literal rule. Assembled from parts so the literals are
# not themselves grep-able as credentials.
AWS_SECRET_40 = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
FIXTURES = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----",
    "aws-access-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "aws-secret-key": "aws_secret_access_key = " + AWS_SECRET_40,
    "anthropic-key": "sk-ant-" + "a" * 32,
    "openai-key": "sk-" + "B" * 32,
    "github-token": "ghp_" + "c" * 36,
    "github-pat": "github_pat_" + "A" * 30,
    "gitlab-token": "glpat-" + "a" * 20,
    "stripe-key": "sk_live_" + "a" * 24,
    "npm-token": "npm_" + "a" * 36,
    "pypi-token": "pypi-AgEIcHlwaS5vcmc" + "A" * 50,
    "sendgrid-key": "SG." + "a" * 22 + "." + "b" * 43,
    "slack-token": "xoxb-" + "1234567890-abcdefghij",
    "slack-webhook": "https://hooks.slack.com/services/T"
    + "ABCDEF12"
    + "/B"
    + "ABCDEF12"
    + "/"
    + "a" * 24,
    "twilio-key": "SK" + "0123456789abcdef" * 2,
    "google-api-key": "AIza" + "D" * 35,
    "jwt": "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20,
    "azure-storage-key": "AccountKey=" + "A" * 86 + "==",
    "bearer-token": "Authorization: Bearer " + "e" * 24,
    "basic-auth-url": "https://user:hunter2pass@example.com/x",
}

# The generic rule is shared too; both gates must agree on these.
GENERIC_HITS = [
    'password = "n0tAr3alP4ssw0rd"',
    'client_secret = "Gx7Qv2Lp9Rt4Wm8Zb3Nc6Yd1Ke5Hf"',
    "DB_PASSWORD=Sup3rS3cret!",
    'my_password_value = "CorrectHorse9"',
]
GENERIC_CLEAN = [
    'password = "changeme"',
    "api_key = your_api_key_here",
    'password = os.environ["APP_PASSWORD"]',
    "token = get_token()",
    'password_file = "/etc/secrets/pw"',
    "# password = whatever you set in the dashboard",
]


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
                self.assertEqual(hit.rule, name)

    def test_more_specific_rule_wins(self):
        # sk-ant- is also matchable by a naive sk- rule; the narrower label is
        # the useful one, so ordering in the table is load-bearing.
        hit = secret_patterns.find(FIXTURES["anthropic-key"])
        self.assertEqual(hit.rule, "anthropic-key")

    def test_value_group_isolates_the_secret_from_its_context(self):
        hit = secret_patterns.find(FIXTURES["aws-secret-key"])
        self.assertEqual(hit.secret, AWS_SECRET_40)
        self.assertEqual(hit.prefix, "aws_secret_access_key = ")
        hit = secret_patterns.find(FIXTURES["basic-auth-url"])
        self.assertEqual(hit.secret, "hunter2pass")


class GenericRuleTests(unittest.TestCase):
    def test_hits(self):
        for text in GENERIC_HITS:
            with self.subTest(line=text):
                hit = secret_patterns.find_generic(text)
                self.assertIsNotNone(hit, f"generic rule missed: {text}")
                self.assertEqual(hit.rule, secret_patterns.GENERIC_RULE)
                self.assertTrue(hit.prefix.endswith(("= ", "=", ": ", ":", '"', "'")))

    def test_clean(self):
        for text in GENERIC_CLEAN:
            with self.subTest(line=text):
                self.assertIsNone(secret_patterns.find_generic(text))

    def test_find_any_prefers_the_literal_label(self):
        hit = secret_patterns.find_any(FIXTURES["aws-secret-key"])
        self.assertEqual(hit.rule, "aws-secret-key")


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

    def test_gates_agree_on_generic_assignments(self):
        for text in GENERIC_HITS:
            with self.subTest(line=text):
                self.assertEqual(len(commit_gate.scan_lines([("f.py", 1, text)])), 1)
                self.assertTrue(push_gate.scan_line(text))
        for text in GENERIC_CLEAN:
            with self.subTest(line=text):
                self.assertEqual(commit_gate.scan_lines([("f.py", 1, text)]), [])
                self.assertEqual(push_gate.scan_line(text), [])

    def test_neither_gate_flags_ordinary_code(self):
        clean = "def main():\n    return 0"
        self.assertEqual(commit_gate.scan_lines([("f.py", 1, clean)]), [])
        self.assertEqual(push_gate.scan_line(clean), [])

    def test_neither_gate_echoes_the_secret(self):
        for name, text in FIXTURES.items():
            if name == "private-key":
                continue
            with self.subTest(rule=name):
                secret = secret_patterns.find(text).secret
                commit_excerpt = commit_gate.scan_lines([("f.py", 1, text)])[0].excerpt
                push_preview = push_gate.scan_line(text)[0][1]
                self.assertNotIn(secret, commit_excerpt)
                self.assertNotIn(secret, push_preview)


class AllowlistParityTests(unittest.TestCase):
    """Both gates read the same ``.secretsallow`` with the same semantics."""

    def test_same_loader(self):
        self.assertIs(commit_gate.load_allowlist, secret_patterns.load_allowlist)
        self.assertIs(commit_gate.Allowlist, secret_patterns.Allowlist)

    def test_path_rule_exempts_file_in_both_gates(self):
        import re

        allow = secret_patterns.Allowlist(paths=(re.compile(r"^fixtures/"),))
        text = FIXTURES["aws-access-key"]
        self.assertEqual(commit_gate.scan_lines([("fixtures/k.py", 1, text)], allow), [])
        diff = f"--- a/x\n+++ b/fixtures/k.py\n@@ -0,0 +1 @@\n+{text}\n"
        self.assertEqual(push_gate.scan_diff(diff, allow), [])
        # Unlisted paths are still scanned by both.
        self.assertEqual(len(commit_gate.scan_lines([("src/k.py", 1, text)], allow)), 1)
        diff = f"--- a/x\n+++ b/src/k.py\n@@ -0,0 +1 @@\n+{text}\n"
        self.assertEqual(len(push_gate.scan_diff(diff, allow)), 1)


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
