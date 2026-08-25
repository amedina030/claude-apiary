#!/usr/bin/env python3
"""Unit tests for pre_push_secret_scan — the pure scanner.

The subprocess shell (_run) shells out to git; the bit that decides *whether
an added line carries a secret* and *which file:line it lives on* is pure and
is where the false-positive / false-negative risk lives, so that's what's
tested here.
"""
import unittest

from core.hooks.pre_push_secret_scan import (
    iter_added_lines,
    scan_diff,
    scan_line,
    _shannon_entropy,
)


def _diff(*added, path="app/config.py"):
    """Build a minimal one-hunk unified diff that adds *added* lines."""
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n{body}"
    )


class ScanLinePositiveTest(unittest.TestCase):
    def test_aws_access_key_id(self):
        self.assertTrue(scan_line("aws_key = AKIAIOSFODNN7EXAMPLE"))

    def test_github_token(self):
        self.assertTrue(scan_line("token=ghp_" + "a" * 36))

    def test_openai_anthropic_key(self):
        self.assertTrue(scan_line('key = "sk-ant-' + "A" * 24 + '"'))
        self.assertTrue(scan_line("k = sk-" + "b" * 22))

    def test_slack_token(self):
        self.assertTrue(scan_line("xoxb-123456789012-abcdefABCDEF"))

    def test_google_api_key(self):
        self.assertTrue(scan_line("AIza" + "B" * 35))

    def test_private_key_block(self):
        self.assertTrue(scan_line("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(scan_line("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertTrue(scan_line("-----BEGIN PRIVATE KEY-----"))

    def test_bearer_token(self):
        self.assertTrue(scan_line("Authorization: Bearer abcdefghij0123456789XY"))

    def test_high_entropy_assignment(self):
        # Real-looking secret: long + high entropy → caught.
        self.assertTrue(scan_line('client_secret = "Gx7Qv2Lp9Rt4Wm8Zb3Nc6Yd1Ke5Hf"'))
        self.assertTrue(scan_line("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX"))


class ScanLineNegativeTest(unittest.TestCase):
    def test_plain_prose_and_code(self):
        for line in (
            "This function pushes a commit to the remote.",
            "def get_token(): return self._token",
            "api_key = your_api_key_here",          # placeholder name pattern
            'password = "changeme"',                 # low entropy
            "token: TODO",                           # too short / low entropy
            "# AKIA is the AWS access-key prefix",   # AKIA without the body
            "sk-",                                    # bare prefix, no body
        ):
            with self.subTest(line=line):
                self.assertEqual(scan_line(line), [])

    def test_allowlist_pragma_suppresses(self):
        line = "aws_key = AKIAIOSFODNN7EXAMPLE  # pragma: allowlist secret"
        self.assertEqual(scan_line(line), [])

    def test_empty_and_none(self):
        self.assertEqual(scan_line(""), [])
        self.assertEqual(scan_line(None), [])

    def test_match_is_redacted_not_echoed(self):
        # The full secret must never appear in the finding (it would re-leak
        # into the block reason / transcript).
        secret = "ghp_" + "z" * 36
        (_rule, preview), = scan_line("t=" + secret)
        self.assertNotIn(secret, preview)
        self.assertIn("…", preview)


class ScanDiffTest(unittest.TestCase):
    def test_reports_file_and_line(self):
        diff = _diff("clean line one", "key = AKIAIOSFODNN7EXAMPLE", path="src/cfg.py")
        findings = scan_diff(diff)
        self.assertEqual(len(findings), 1)
        path, lineno, rule, _preview = findings[0]
        self.assertEqual(path, "src/cfg.py")
        self.assertEqual(lineno, 2)            # second added line
        self.assertEqual(rule, "aws-access-key")

    def test_removed_lines_are_ignored(self):
        # A secret on a removed (-) line is not being introduced → no finding.
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n"
            "-old = AKIAIOSFODNN7EXAMPLE\n+new = clean\n"
        )
        self.assertEqual(scan_diff(diff), [])

    def test_clean_diff(self):
        self.assertEqual(scan_diff(_diff("just some", "ordinary code")), [])

    def test_line_numbers_follow_hunk_header(self):
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -40,2 +40,3 @@\n"
            " context line\n"
            "+secret = ghp_" + "q" * 36 + "\n"
            " trailing context\n"
        )
        (path, lineno, rule, _), = scan_diff(diff)
        self.assertEqual(lineno, 41)           # 40 (context) then +1
        self.assertEqual(rule, "github-token")


class IterAddedLinesTest(unittest.TestCase):
    def test_strips_plus_and_tracks_path(self):
        rows = list(iter_added_lines(_diff("alpha", "beta", path="d/f.py")))
        self.assertEqual([(p, c) for p, _, c in rows],
                         [("d/f.py", "alpha"), ("d/f.py", "beta")])

    def test_plus_plus_header_not_treated_as_added(self):
        # The "+++ b/file" header starts with + but must not be scanned.
        rows = list(iter_added_lines(_diff("only real add")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "only real add")


class EntropyTest(unittest.TestCase):
    def test_placeholder_below_threshold(self):
        self.assertLess(_shannon_entropy("changeme"), 4.0)
        self.assertLess(_shannon_entropy("your_api_key_here"), 4.0)

    def test_real_secret_above_threshold(self):
        self.assertGreaterEqual(_shannon_entropy("Gx7Qv2Lp9Rt4Wm8Zb3Nc6Yd1Ke5Hf"), 4.0)

    def test_empty(self):
        self.assertEqual(_shannon_entropy(""), 0.0)


if __name__ == "__main__":
    unittest.main()
