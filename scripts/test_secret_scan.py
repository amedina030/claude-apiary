#!/usr/bin/env python3
"""Tests for scripts/secret_scan.py.

The fixtures below contain deliberately fake credentials — that is the point
of the suite. ``.secretsallow`` in the repo root exempts this file, otherwise
the hook would block every commit that touches it.
"""

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import secret_scan  # noqa: E402

GENERIC = secret_scan.PATTERNS[-1].name

# --- fake credentials, assembled so the literals aren't themselves scannable
# by a naive grep of this file. Each is syntactically valid for its pattern
# but corresponds to nothing real.
AWS_SECRET_40 = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"  # 40 chars, AWS docs example
FAKE = {
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
    GENERIC: 'password = "n0tAr3alP4ssw0rd"',
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
        found = secret_scan.scan_lines([("src/app.py", 42, FAKE["aws-access-key"])])
        self.assertEqual(len(found), 1)
        rendered = found[0].render()
        self.assertIn("src/app.py:42", rendered)
        self.assertIn("aws-access-key", rendered)


class RedactionTests(unittest.TestCase):
    """The report is printed to a terminal and, via the push gate, into a
    Claude transcript. It must never reprint the credential it found."""

    def test_no_fixture_secret_appears_in_its_own_finding(self):
        for name, text in FAKE.items():
            if name in ("private-key",):  # a header, not a value
                continue
            with self.subTest(pattern=name):
                found = secret_scan.scan_lines(_lines(text))
                secret = (
                    text.split("=", 1)[-1].split()[-1]
                    if name in (GENERIC, "aws-secret-key")
                    else text
                )
                if name == "bearer-token":
                    secret = text.split()[-1]
                if name == "basic-auth-url":
                    secret = "hunter2pass"
                if name == "azure-storage-key":
                    secret = text.split("=", 1)[1]
                self.assertNotIn(secret, found[0].excerpt)
                self.assertNotIn(secret, found[0].render())
                self.assertIn("…", found[0].excerpt)

    def test_generic_finding_keeps_the_key_name_readable(self):
        found = secret_scan.scan_lines(_lines('db_password = "n0tAr3alP4ssw0rd"'))
        self.assertTrue(found[0].excerpt.startswith("db_password = "))
        self.assertNotIn("n0tAr3alP4ssw0rd", found[0].excerpt)

    def test_short_secret_is_still_redacted(self):
        self.assertEqual(secret_scan._redact("abcdefgh"), "ab***")
        self.assertNotIn("cdefgh", secret_scan._redact("abcdefgh"))


class FormerlyMissedTests(unittest.TestCase):
    """Shapes the first version of the scanner let through. Each was a real
    gap found in review (2026-08); none may regress."""

    MUST_FLAG = [
        ("aws_secret_access_key = " + AWS_SECRET_40, "aws-secret-key"),
        ('AWS_SECRET_ACCESS_KEY="' + AWS_SECRET_40 + '"', "aws-secret-key"),
        ("aws-secret-key: " + AWS_SECRET_40, "aws-secret-key"),
        ('password = "Tr0ub4dor&3xyz"', GENERIC),  # punctuation in value
        ('password = "p@ssw0rd!2024"', GENERIC),
        ('token = "abcdefgh12345678"  # see get_config()', GENERIC),  # call in the comment
        ('my_password_value = "CorrectHorse9"', GENERIC),  # key with prefix + suffix
        ('secret_key = "django-insecure-abc123def456"', GENERIC),
        ('"password": "p@ssw0rd!2024",', GENERIC),  # JSON
        ("DB_PASSWORD=Sup3rS3cret!", GENERIC),  # bare, env-file style
        ("--password=abc12345xyz", GENERIC),  # CLI flag in a script
        ('self._token = "abcdefgh12345678"', GENERIC),  # private attribute
        ("password: 'correct horse battery staple'", GENERIC),  # quoted, with spaces
        ("github_pat_" + "A" * 30, "github-pat"),
        ("sk_live_" + "a" * 24, "stripe-key"),
        ("rk_test_" + "a" * 24, "stripe-key"),
        ("glpat-" + "a" * 20, "gitlab-token"),
        ("npm_" + "a" * 36, "npm-token"),
        ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "private-key"),
        ("-----BEGIN ENCRYPTED PRIVATE KEY-----", "private-key"),
    ]

    def test_each_formerly_missed_shape_is_flagged(self):
        for text, expected in self.MUST_FLAG:
            with self.subTest(line=text):
                found = secret_scan.scan_lines(_lines(text))
                self.assertEqual(len(found), 1, f"missed: {text}")
                self.assertEqual(found[0].pattern, expected)


class FalsePositiveTests(unittest.TestCase):
    """The generic assignment rule is the broad one; keep it quiet."""

    CLEAN = [
        'password = os.environ["APP_PASSWORD"]',
        "token = get_token()",
        "token = get_token()  # returns the token",
        "api_key = os.getenv('API_KEY')",
        'secret = "your-secret-here"',
        'password = "changeme"',
        "port = 8080",
        "timeout = 900",
        'token = f"{prefix}-{suffix}"',
        'password = "${DB_PASSWORD}"',
        'password = "%(DB_PASSWORD)s"',
        "token: ${GITHUB_TOKEN}",
        "export TOKEN=$(cat ~/.token)",
        "client_secret = config.get('client_secret')",
        'password_confirm = request.form["password_confirm"]',
        "password = settings.db.password",
        "# password = whatever you set in the dashboard",
        "the password is stored in the vault",
        "access_key = process.env.ACCESS_KEY",
        'password_file = "/etc/secrets/pw"',
        'token_url = "https://example.com/oauth/token"',
        "token_endpoint = https://example.com/oauth/token",
        'secret_name = "my-vault-secret-name"',
        'api_key_header = "X-API-Key-Value"',
        'token_description = "used for the nightly job"',
        "PASSWORD_MIN_LENGTH = 12",
        "token_count = 12345678",
        'password = "********"',
        'api_key = "<your-api-key>"',
        'password = "[REDACTED]"',
        'secret = "xxxxxxxxxxxxxxxx"',
        'password = "abababababababab"',
        'api_key = "my_api_key_here"',
        "password: {{ vault_pw }}",
        "ns.token_cap = token_cap",  # bare identifier: a read, not a literal
        "password = new_password",
        "self.token = token",
        "f(token_threshold=token_threshold, x=1)",  # identifier followed by more args
        '"token": "montecarlodata",',  # JSON search/lexer token: a plain word
        "tokens = ['datadog', 'snowflake']",
    ]

    def test_bare_token_key_still_fires_on_a_credential_shaped_value(self):
        # The `token`-alone relaxation only exempts plain words.
        self.assertEqual(len(secret_scan.scan_lines(_lines('"token": "d4t4d0g-X9"'))), 1)
        self.assertEqual(len(secret_scan.scan_lines(_lines('auth_token = "montecarlodata"'))), 1)

    def test_clean_lines_do_not_trigger(self):
        for line in self.CLEAN:
            with self.subTest(line=line):
                self.assertEqual(secret_scan.scan_lines(_lines(line)), [])

    def test_lockfiles_are_skipped(self):
        found = secret_scan.scan_lines([("poetry.lock", 1, FAKE["aws-access-key"])])
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
        both = f"{FAKE['aws-access-key']} {FAKE['github-token']}"
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
        rules = [re.compile(r"^fixtures/")]
        found = secret_scan.scan_lines([("fixtures/keys.txt", 1, FAKE["aws-access-key"])], rules)
        self.assertEqual(found, [])
        # A different path is still scanned.
        found = secret_scan.scan_lines([("src/keys.txt", 1, FAKE["aws-access-key"])], rules)
        self.assertEqual(len(found), 1)

    def test_path_rule_does_not_leak_into_line_matching(self):
        # A loose path regex used to be tested against the LINE too, so an
        # entry like `fixtures` silenced any line containing that word.
        rules = secret_scan.Allowlist(paths=(re.compile("fixtures"),))
        line = f'k = "{FAKE["aws-access-key"]}"  # from fixtures'
        self.assertEqual(len(secret_scan.scan_lines([("src/a.py", 1, line)], rules)), 1)

    def test_line_rule_matches_lines_only(self):
        rules = secret_scan.Allowlist(lines=(re.compile("EXAMPLE"),))
        self.assertEqual(
            secret_scan.scan_lines([("src/a.py", 1, FAKE["aws-access-key"])], rules), []
        )
        # ...and does not exempt a path that happens to match.
        found = secret_scan.blocked_files(["EXAMPLE/.env"], rules)
        self.assertEqual(len(found), 1)

    def test_allowlist_file_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / secret_scan.ALLOWLIST_FILENAME).write_text(
                "# a comment\n\n^docs/\nline: EXAMPLE\n[unclosed\n", encoding="utf-8"
            )
            rules = secret_scan.load_allowlist(root)
            self.assertEqual(len(rules.paths), 1)
            self.assertEqual(len(rules.lines), 1)
            self.assertTrue(rules.allows_path("docs/x.md"))
            self.assertTrue(rules.allows_line("has EXAMPLE in it"))
            self.assertFalse(rules.allows_line("docs/x.md"))

    def test_missing_allowlist_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(secret_scan.load_allowlist(Path(tmp)), secret_scan.Allowlist())


class BlockedFileTests(unittest.TestCase):
    def test_dotenv_is_blocked(self):
        found = secret_scan.blocked_files([".env"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "blocked-file")

    def test_credential_filenames_blocked_but_lookalikes_allowed(self):
        blocked = [
            ".env",
            ".env.local",
            "cfg/.env.production",
            "id_rsa",
            "a/b/key.pem",
            "server.key",
            "deploy.ppk",
            "AuthKey_ABC.p8",
            ".netrc",
            ".git-credentials",
            ".htpasswd",
            "ops/kubeconfig",
            ".docker/config.json",
            "gcp/service-account-prod.json",
            "credentials.json",
            ".aws/credentials",
        ]
        allowed = [
            ".env.example",
            ".env.sample",
            ".env.template",
            "notes.md",
            "keys.md",
            "public.pub",
            "docs/credentials.md",
            "src/keystore.py",
            "monkey.js",
        ]
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
        diff = f"--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n-{FAKE['aws-access-key']}\n+clean line\n"
        added = secret_scan.parse_staged_diff(diff)
        self.assertEqual(added, [("x.py", 1, "clean line")])
        self.assertEqual(secret_scan.scan_lines(added), [])

    def test_deleted_file_contributes_nothing(self):
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"  # portability-ok: git diff syntax, not a path we open
        self.assertEqual(secret_scan.parse_staged_diff(diff), [])

    def test_quoted_path_header_is_unquoted(self):
        # git C-quotes paths with unusual characters: +++ "b/we\"ird.py"
        diff = '--- a/x\n+++ "b/we\\"ird.py"\n@@ -0,0 +1 @@\n+line\n'
        self.assertEqual(secret_scan.parse_staged_diff(diff), [('we"ird.py', 1, "line")])


class EntropyTests(unittest.TestCase):
    RANDOM = "Xq7bZ2mK9pLw3RtY8vNc4JhF6sDgA1eU5o"

    def test_entropy_off_by_default(self):
        self.assertEqual(secret_scan.scan_lines(_lines(f"blob = {self.RANDOM}")), [])

    def test_entropy_flags_when_enabled(self):
        found = secret_scan.scan_lines(_lines(f"blob = {self.RANDOM}"), entropy=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "high-entropy")

    def test_prose_is_not_high_entropy(self):
        line = "the quick brown fox jumps over the lazy dog repeatedly today"
        self.assertEqual(secret_scan.scan_lines(_lines(line), entropy=True), [])


def _run_git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


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
        (self.root / secret_scan.ALLOWLIST_FILENAME).write_text("^fixtures/\n", encoding="utf-8")
        self._stage("fixtures/sample.txt", FAKE["aws-access-key"] + "\n")
        self.assertEqual(secret_scan.scan_staged(self.root), [])

    def test_unstaged_changes_are_not_scanned(self):
        # Written but never `git add`-ed: not part of this commit.
        (self.root / "loose.py").write_text(f"k = '{FAKE['aws-access-key']}'\n", encoding="utf-8")
        self.assertEqual(secret_scan.scan_staged(self.root), [])


class FailClosedTests(unittest.TestCase):
    """If git cannot be run, the scan must not report "clean"."""

    def test_git_failure_raises_instead_of_returning_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Not a git repo: `git diff --cached` exits non-zero.
            with self.assertRaises(secret_scan.GitError):
                secret_scan.scan_staged(Path(tmp))

    def test_staged_mode_exits_2_and_says_the_scan_did_not_run(self):
        err = io.StringIO()
        with (
            mock.patch.object(secret_scan, "repo_root", return_value=Path(".")),
            mock.patch.object(
                secret_scan, "_git", side_effect=secret_scan.GitError("index.lock exists")
            ),
            contextlib.redirect_stderr(err),
        ):
            code = secret_scan.main(["--staged"])
        self.assertEqual(code, 2)
        self.assertIn("did NOT run", err.getvalue())
        self.assertIn("index.lock", err.getvalue())

    def test_git_missing_entirely_is_also_fatal(self):
        with mock.patch.object(secret_scan.subprocess, "run", side_effect=OSError("no git")):
            with self.assertRaises(secret_scan.GitError):
                secret_scan._git(["diff"], Path("."))
            # ...but repo_root, used to decide whether we are in a repo at all,
            # degrades to None so --path mode still works outside git.
            self.assertIsNone(secret_scan.repo_root(Path(".")))


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
            (root / ".git" / "cfg").write_text(FAKE["aws-access-key"], encoding="utf-8")
            self.assertEqual(secret_scan.scan_path(root), [])

    def test_gitignored_files_are_skipped_inside_a_repo(self):
        # Runtime logs a commit can never include are noise for a tree scan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_git(["init", "-q"], root)
            (root / ".gitignore").write_text("logs/\n", encoding="utf-8")
            (root / "logs").mkdir()
            (root / "logs" / "run.log").write_text(FAKE["aws-access-key"], encoding="utf-8")
            (root / "src.py").write_text(f"k = '{FAKE['github-token']}'\n", encoding="utf-8")
            found = secret_scan.scan_path(root, root)
            self.assertEqual([f.path for f in found], ["src.py"])
            # Without a repo root the walk is plain and sees everything.
            self.assertEqual(len(secret_scan.scan_path(root)), 2)


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
