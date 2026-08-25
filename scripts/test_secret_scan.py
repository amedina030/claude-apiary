#!/usr/bin/env python3
"""Tests for scripts/secret_scan.py.

The fixtures below contain deliberately fake credentials — that is the point
of the suite. ``.secretsallow`` in the repo root exempts this file, otherwise
the hook would block every commit that touches it.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import secret_scan  # noqa: E402

# --- fake credentials, assembled so the literals aren't themselves scannable
# by a naive grep of this file. Each is syntactically valid for its pattern
# but corresponds to nothing real.
FAKE = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----",
    "aws-access-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "anthropic-key": "sk-ant-" + "a" * 32,
    "openai-key": "sk-" + "B" * 32,
    "github-token": "ghp_" + "c" * 36,
    "slack-token": "xoxb-" + "1234567890-abcdefghij",
    "google-api-key": "AIza" + "D" * 35,
    "bearer-token": "Authorization: Bearer " + "e" * 24,
    "basic-auth-url": "https://user:hunter2pass@example.com/x",
    "generic-assignment": 'password = "n0tAr3alP4ssw0rd"',
}


def _lines(*texts: str, path: str = "f.py") -> list:
    return [(path, i, t) for i, t in enumerate(texts, start=1)]


class PatternTests(unittest.TestCase):
    """One positive case per pattern in the table."""

    def test_every_pattern_has_a_positive_case(self):
        names = {p.name for p in secret_scan.PATTERNS}
        self.assertEqual(
            names,
            set(FAKE),
            "every pattern needs a fixture (and vice versa) so the table stays covered",
        )

    def test_each_pattern_matches_its_fixture(self):
        for name, text in FAKE.items():
            with self.subTest(pattern=name):
                found = secret_scan.scan_lines(_lines(text))
                self.assertEqual(len(found), 1, f"{name} did not match: {text}")
                self.assertEqual(found[0].pattern, name)
                self.assertEqual(found[0].line_no, 1)
                self.assertEqual(found[0].path, "f.py")

    def test_finding_reports_file_and_line(self):
        found = secret_scan.scan_lines(
            [("src/app.py", 42, FAKE["aws-access-key"])]
        )
        self.assertEqual(len(found), 1)
        rendered = found[0].render()
        self.assertIn("src/app.py:42", rendered)
        self.assertIn("aws-access-key", rendered)

    def test_long_match_is_truncated_in_output(self):
        found = secret_scan.scan_lines(_lines("k = " + FAKE["anthropic-key"] * 6))
        self.assertTrue(found[0].excerpt.endswith("…"))
        self.assertLess(len(found[0].excerpt), 120)


class FalsePositiveTests(unittest.TestCase):
    """The generic assignment rule is the broad one; keep it quiet."""

    CLEAN = [
        'password = os.environ["APP_PASSWORD"]',
        "token = get_token()",
        "api_key = os.getenv('API_KEY')",
        'secret = "your-secret-here"',
        'password = "changeme"',
        "port = 8080",
        "timeout = 900",
        'token = f"{prefix}-{suffix}"',
        'password = "${DB_PASSWORD}"',
        "client_secret = config.get('client_secret')",
        "# password = whatever you set in the dashboard",
        'access_key = process.env.ACCESS_KEY',
    ]

    def test_clean_lines_do_not_trigger(self):
        for line in self.CLEAN:
            with self.subTest(line=line):
                self.assertEqual(secret_scan.scan_lines(_lines(line)), [])

    def test_lockfiles_are_skipped(self):
        found = secret_scan.scan_lines(
            [("poetry.lock", 1, FAKE["aws-access-key"])]
        )
        self.assertEqual(found, [])

    def test_binary_extensions_are_skipped(self):
        found = secret_scan.scan_lines([("logo.png", 1, FAKE["github-token"])])
        self.assertEqual(found, [])

    def test_quoted_wordy_value_is_still_flagged(self):
        # The prose filter only relaxes UNQUOTED values. An explicit string
        # literal is a hardcoded credential however word-like it reads.
        found = secret_scan.scan_lines(_lines('password = "supersecretvalue"'))
        self.assertEqual(len(found), 1)

    def test_bare_word_needs_a_credential_signal(self):
        self.assertEqual(secret_scan.scan_lines(_lines("password = whatever")), [])
        # ...but a bare value with a digit or punctuation still counts.
        self.assertEqual(len(secret_scan.scan_lines(_lines("password = wh4tever1"))), 1)

    def test_one_finding_per_line(self):
        both = f'{FAKE["aws-access-key"]} {FAKE["github-token"]}'
        self.assertEqual(len(secret_scan.scan_lines(_lines(both))), 1)


class AllowlistTests(unittest.TestCase):
    def test_inline_pragma_suppresses(self):
        line = f'key = "{FAKE["aws-access-key"]}"  # {secret_scan.ALLOW_PRAGMA}'
        self.assertEqual(secret_scan.scan_lines(_lines(line)), [])

    def test_pragma_works_with_any_comment_syntax(self):
        line = f'const k = "{FAKE["github-token"]}"; // {secret_scan.ALLOW_PRAGMA}'
        self.assertEqual(secret_scan.scan_lines(_lines(line)), [])

    def test_push_gate_pragma_is_honoured(self):
        # core/hooks/pre_push_secret_scan.py uses the detect-secrets spelling.
        # A line silenced for that gate must not be re-flagged by this one.
        line = f'key = "{FAKE["aws-access-key"]}"  # pragma: allowlist secret'
        self.assertEqual(secret_scan.scan_lines(_lines(line)), [])

    def test_allowlist_regex_by_path(self):
        rules = [__import__("re").compile(r"^fixtures/")]
        found = secret_scan.scan_lines(
            [("fixtures/keys.txt", 1, FAKE["aws-access-key"])], rules
        )
        self.assertEqual(found, [])
        # A different path is still scanned.
        found = secret_scan.scan_lines(
            [("src/keys.txt", 1, FAKE["aws-access-key"])], rules
        )
        self.assertEqual(len(found), 1)

    def test_allowlist_file_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / secret_scan.ALLOWLIST_FILENAME).write_text(
                "# a comment\n\n^docs/\n[unclosed\n", encoding="utf-8"
            )
            rules = secret_scan.load_allowlist(root)
            # The invalid regex is skipped, the valid one survives.
            self.assertEqual(len(rules), 1)
            self.assertTrue(rules[0].search("docs/x.md"))

    def test_missing_allowlist_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(secret_scan.load_allowlist(Path(tmp)), [])


class BlockedFileTests(unittest.TestCase):
    def test_dotenv_is_blocked(self):
        found = secret_scan.blocked_files([".env"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "blocked-file")

    def test_dotenv_variants_blocked_but_examples_allowed(self):
        blocked = [".env", ".env.local", "cfg/.env.production", "id_rsa", "a/b/key.pem"]
        allowed = [".env.example", ".env.sample", ".env.template", "notes.md"]
        for p in blocked:
            with self.subTest(path=p):
                self.assertEqual(len(secret_scan.blocked_files([p])), 1)
        for p in allowed:
            with self.subTest(path=p):
                self.assertEqual(secret_scan.blocked_files([p]), [])


class DiffParsingTests(unittest.TestCase):
    DIFF = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+first added\n"
        "+second added\n"
        "@@ -10,0 +20,1 @@\n"
        "+much later\n"
    )

    def test_added_lines_get_post_image_numbers(self):
        added = secret_scan.parse_staged_diff(self.DIFF)
        self.assertEqual(
            added,
            [
                ("app.py", 1, "first added"),
                ("app.py", 2, "second added"),
                ("app.py", 20, "much later"),
            ],
        )

    def test_removed_lines_are_ignored(self):
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,1 @@\n"
            f"-{FAKE['aws-access-key']}\n"
            "+clean line\n"
        )
        added = secret_scan.parse_staged_diff(diff)
        self.assertEqual(added, [("x.py", 1, "clean line")])
        self.assertEqual(secret_scan.scan_lines(added), [])

    def test_deleted_file_contributes_nothing(self):
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"  # noqa: null-device
        self.assertEqual(secret_scan.parse_staged_diff(diff), [])


class EntropyTests(unittest.TestCase):
    RANDOM = "Xq7bZ2mK9pLw3RtY8vNc4JhF6sDgA1eU5o"

    def test_entropy_off_by_default(self):
        self.assertEqual(secret_scan.scan_lines(_lines(f"blob = {self.RANDOM}")), [])

    def test_entropy_flags_when_enabled(self):
        found = secret_scan.scan_lines(
            _lines(f"blob = {self.RANDOM}"), entropy=True
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "high-entropy")

    def test_prose_is_not_high_entropy(self):
        line = "the quick brown fox jumps over the lazy dog repeatedly today"
        self.assertEqual(secret_scan.scan_lines(_lines(line), entropy=True), [])


def _run_git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


class StagedIntegrationTests(unittest.TestCase):
    """End-to-end against a real throwaway git repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _run_git(["init", "-q"], self.root)
        _run_git(["config", "user.email", "t@example.com"], self.root)
        _run_git(["config", "user.name", "T"], self.root)
        _run_git(["config", "commit.gpgsign", "false"], self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _stage(self, name, content, force=False):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        args = ["add", "-f", name] if force else ["add", name]
        _run_git(args, self.root)

    def test_clean_staged_diff_passes(self):
        self._stage("ok.py", "print('hello')\n")
        self.assertEqual(secret_scan.scan_staged(self.root), [])

    def test_staged_secret_is_found_with_correct_line(self):
        self._stage("cfg.py", f"a = 1\nb = 2\nkey = '{FAKE['aws-access-key']}'\n")
        found = secret_scan.scan_staged(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "cfg.py")
        self.assertEqual(found[0].line_no, 3)

    def test_force_added_dotenv_is_blocked(self):
        (self.root / ".gitignore").write_text(".env\n", encoding="utf-8")
        self._stage(".env", "TOKEN=abc\n", force=True)
        found = secret_scan.scan_staged(self.root)
        self.assertTrue(any(f.pattern == "blocked-file" for f in found))

    def test_repo_allowlist_file_applies(self):
        (self.root / secret_scan.ALLOWLIST_FILENAME).write_text(
            "^fixtures/\n", encoding="utf-8"
        )
        self._stage("fixtures/sample.txt", FAKE["aws-access-key"] + "\n")
        self.assertEqual(secret_scan.scan_staged(self.root), [])

    def test_unstaged_changes_are_not_scanned(self):
        # Written but never `git add`-ed: not part of this commit.
        (self.root / "loose.py").write_text(
            f"k = '{FAKE['aws-access-key']}'\n", encoding="utf-8"
        )
        self.assertEqual(secret_scan.scan_staged(self.root), [])


class PathScanTests(unittest.TestCase):
    def test_scan_path_walks_a_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "bad.py").write_text(
                f"k = '{FAKE['github-token']}'\n", encoding="utf-8"
            )
            (root / "good.py").write_text("x = 1\n", encoding="utf-8")
            found = secret_scan.scan_path(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].path, "sub/bad.py")

    def test_scan_path_accepts_a_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "one.py"
            f.write_text(f"k = '{FAKE['openai-key']}'\n", encoding="utf-8")
            self.assertEqual(len(secret_scan.scan_path(f)), 1)

    def test_git_directory_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "cfg").write_text(
                FAKE["aws-access-key"], encoding="utf-8"
            )
            self.assertEqual(secret_scan.scan_path(root), [])


class CliTests(unittest.TestCase):
    def test_exit_code_1_on_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.py"
            f.write_text(FAKE["aws-access-key"], encoding="utf-8")
            self.assertEqual(secret_scan.main(["--path", str(f)]), 1)

    def test_exit_code_0_on_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.py"
            f.write_text("nothing here\n", encoding="utf-8")
            self.assertEqual(secret_scan.main(["--path", str(f), "--quiet"]), 0)

    def test_missing_path_is_a_usage_error(self):
        self.assertEqual(secret_scan.main(["--path", "no/such/place"]), 2)


if __name__ == "__main__":
    unittest.main()
