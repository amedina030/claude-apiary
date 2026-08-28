#!/usr/bin/env python3
"""Unit tests for pre_push_secret_scan — the pure scanner plus the push parser.

The subprocess shell (_run) shells out to git; the bits that decide *whether
an added line carries a secret*, *which file:line:commit it lives on*, and
*what is being pushed* are pure and are where the false-positive /
false-negative risk lives, so that's what's tested here. One integration test
drives the same git invocation ``_run`` uses against a throwaway repo.

Fixtures are fake credentials; ``.secretsallow`` exempts this file.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.hooks.pre_push_secret_scan import (
    UNRESOLVED_CWD,
    PushTarget,
    _shannon_entropy,
    iter_added_lines,
    outgoing_log_args,
    push_target,
    push_targets,
    scan_diff,
    scan_line,
    scan_patch_series,
)

AWS_ID = "AKIA" + "IOSFODNN7EXAMPLE"


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
        self.assertTrue(scan_line(f"aws_key = {AWS_ID}"))

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
        self.assertTrue(scan_line("-----BEGIN PGP PRIVATE KEY BLOCK-----"))

    def test_bearer_token(self):
        self.assertTrue(scan_line("Authorization: Bearer abcdefghij0123456789XY"))

    def test_credential_assignments(self):
        self.assertTrue(scan_line('client_secret = "Gx7Qv2Lp9Rt4Wm8Zb3Nc6Yd1Ke5Hf"'))
        self.assertTrue(scan_line("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX"))
        # Hex and short values used to slip under the old entropy bar.
        self.assertTrue(scan_line('api_key = "0123456789abcdef0123456789abcdef"'))
        self.assertTrue(scan_line('password = "Tr0ub4dor&3xyz"'))


class ScanLineNegativeTest(unittest.TestCase):
    def test_plain_prose_and_code(self):
        for line in (
            "This function pushes a commit to the remote.",
            "def get_token(): return self._token",
            "api_key = your_api_key_here",  # placeholder name pattern
            'password = "changeme"',  # placeholder
            "token: TODO",  # placeholder / too short
            "# AKIA is the AWS access-key prefix",  # AKIA without the body
            "sk-",  # bare prefix, no body
            'password_file = "/etc/secrets/pw"',  # about a credential, not one
            "password = settings.db.password",  # a read, not a literal
        ):
            with self.subTest(line=line):
                self.assertEqual(scan_line(line), [])

    def test_allowlist_pragma_suppresses(self):
        for marker in ("pragma: allowlist secret", "apiary:allow-secret"):
            with self.subTest(marker=marker):
                self.assertEqual(scan_line(f"aws_key = {AWS_ID}  # {marker}"), [])

    def test_empty_and_none(self):
        self.assertEqual(scan_line(""), [])
        self.assertEqual(scan_line(None), [])

    def test_match_is_redacted_not_echoed(self):
        # The full secret must never appear in the finding (it would re-leak
        # into the block reason / transcript).
        secret = "ghp_" + "z" * 36
        ((_rule, preview),) = scan_line("t=" + secret)
        self.assertNotIn(secret, preview)
        self.assertIn("…", preview)

    def test_generic_preview_keeps_the_key_and_redacts_the_value(self):
        ((rule, preview),) = scan_line('db_password = "n0tAr3alP4ssw0rd"')
        self.assertEqual(rule, "generic-assignment")
        self.assertTrue(preview.startswith("db_password = "))
        self.assertNotIn("n0tAr3alP4ssw0rd", preview)


class ScanDiffTest(unittest.TestCase):
    def test_reports_file_and_line(self):
        diff = _diff("clean line one", f"key = {AWS_ID}", path="src/cfg.py")
        findings = scan_diff(diff)
        self.assertEqual(len(findings), 1)
        path, lineno, rule, _preview = findings[0]
        self.assertEqual(path, "src/cfg.py")
        self.assertEqual(lineno, 2)  # second added line
        self.assertEqual(rule, "aws-access-key")

    def test_removed_lines_are_ignored(self):
        # A secret on a removed (-) line is not being introduced → no finding.
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n"
            f"-old = {AWS_ID}\n+new = clean\n"
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
        ((path, lineno, rule, _),) = scan_diff(diff)
        self.assertEqual(lineno, 41)  # 40 (context) then +1
        self.assertEqual(rule, "github-token")

    def test_quoted_path_header_is_unquoted(self):
        diff = '--- a/x\n+++ "b/we\\"ird.py"\n@@ -0,0 +1 @@\n+k = ' + AWS_ID + "\n"
        ((path, _, _, _),) = scan_diff(diff)
        self.assertEqual(path, 'we"ird.py')


class IterAddedLinesTest(unittest.TestCase):
    def test_strips_plus_and_tracks_path(self):
        rows = list(iter_added_lines(_diff("alpha", "beta", path="d/f.py")))
        self.assertEqual([(p, c) for p, _, c in rows], [("d/f.py", "alpha"), ("d/f.py", "beta")])

    def test_plus_plus_header_not_treated_as_added(self):
        # The "+++ b/file" header starts with + but must not be scanned.
        rows = list(iter_added_lines(_diff("only real add")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "only real add")


class ScanPatchSeriesTest(unittest.TestCase):
    """``git log -p --format=%H`` output: one patch per outgoing commit."""

    SHA_A = "a" * 40
    SHA_B = "b" * 40

    def test_secret_added_then_removed_is_still_reported(self):
        # Commit A adds the secret, commit B deletes it. A cumulative
        # base..HEAD diff shows nothing; the history being pushed still
        # carries it, so the per-commit scan must report it against A.
        log = (
            f"{self.SHA_A}\n\n"
            + _diff(f"key = {AWS_ID}", path="cfg.py")
            + f"{self.SHA_B}\n\n"
            + "diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n"
            f"@@ -1,1 +0,0 @@\n-key = {AWS_ID}\n"
        )
        findings = scan_patch_series(log)
        self.assertEqual(len(findings), 1)
        sha, path, lineno, rule, _ = findings[0]
        self.assertEqual((sha, path, lineno, rule), (self.SHA_A, "cfg.py", 1, "aws-access-key"))

    def test_same_secret_in_two_commits_is_reported_once(self):
        log = (
            f"{self.SHA_A}\n\n"
            + _diff(f"key = {AWS_ID}", path="cfg.py")
            + f"{self.SHA_B}\n\n"
            + _diff("x = 1", f"key = {AWS_ID}", path="cfg.py")
        )
        findings = scan_patch_series(log)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], self.SHA_A)

    def test_plain_diff_without_sha_lines_still_scans(self):
        findings = scan_patch_series(_diff(f"key = {AWS_ID}"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "")

    def test_repo_allowlist_is_honoured(self):
        import re

        from core.secret_patterns import Allowlist

        log = f"{self.SHA_A}\n\n" + _diff(f"key = {AWS_ID}", path="tests/fixtures.py")
        by_path = Allowlist(paths=(re.compile(r"^tests/"),))
        by_line = Allowlist(lines=(re.compile("EXAMPLE"),))
        self.assertEqual(scan_patch_series(log, by_path), [])
        self.assertEqual(scan_patch_series(log, by_line), [])
        # A path rule does not leak into line matching and vice versa.
        self.assertEqual(len(scan_patch_series(log, Allowlist(paths=(re.compile("EXAMPLE"),)))), 1)
        self.assertEqual(len(scan_patch_series(log, Allowlist(lines=(re.compile(r"^tests/"),)))), 1)


class PushTargetTest(unittest.TestCase):
    def test_bare_push_scans_head(self):
        self.assertEqual(push_target("git push"), PushTarget(None, ("HEAD",), False, None))

    def test_named_remote_and_branch(self):
        t = push_target("git push origin feature/x")
        self.assertEqual((t.remote, t.refs, t.everything), ("origin", ("feature/x",), False))

    def test_options_and_refspec_forms(self):
        self.assertEqual(push_target("git push -u origin HEAD").refs, ("HEAD",))
        self.assertEqual(
            push_target("git push --force-with-lease origin +local:remote").refs, ("local",)
        )
        self.assertEqual(push_target("git push origin tag v1.2").refs, ("v1.2",))
        self.assertEqual(
            push_target("git push origin :gone").refs, ()
        )  # deletion → nothing outgoing
        self.assertEqual(push_target("git push origin --delete gone").refs, ())
        self.assertEqual(push_target("git push -d origin gone").refs, ())
        self.assertEqual(push_target("git push origin :gone main").refs, ("main",))
        self.assertEqual(push_target("git push origin").refs, ("HEAD",))
        self.assertEqual(push_target("git push -o ci.skip origin main").refs, ("main",))

    def test_all_and_mirror_scan_every_branch(self):
        self.assertTrue(push_target("git push --all origin").everything)
        self.assertTrue(push_target("git push --mirror").everything)

    def test_url_remote_is_not_treated_as_a_name(self):
        t = push_target("git push https://example.com/r.git main")
        self.assertEqual((t.remote, t.refs, t.url), (None, ("main",), "https://example.com/r.git"))
        t = push_target("git push git@github.com:o/r.git main")
        self.assertIsNone(t.remote)
        # scp-style and bare-path destinations are URLs too (no remote-tracking refs).
        # Backslash paths are eaten by posix shlex (L-2026-70); only the
        # forward-slash Windows form is testable here.
        for dest in (
            "deploy@prod:/srv/app",
            "/srv/mirrors/publicdir",
            "../bare",
            "~/repos/x",
            "C:/repos/bare",
            "//nas/share/repo",
        ):
            with self.subTest(dest=dest):
                t = push_target(f"git push {dest} main")
                self.assertEqual((t.remote, t.url), (None, dest))
        self.assertEqual(push_target("git push origin main").url, None)

    def test_git_token_forms_and_newlines(self):
        self.assertEqual(push_target("git.exe push origin main").remote, "origin")
        self.assertEqual(push_target("/usr/bin/git push origin main").remote, "origin")
        self.assertEqual(push_target("C:/Git/bin/git.exe push origin main").remote, "origin")
        self.assertEqual(push_target("git --exec-path /x push origin main").remote, "origin")
        ts = push_targets('git commit -m "wip"\ngit push origin main')
        self.assertEqual([(t.remote, t.refs) for t in ts], [("origin", ("main",))])
        self.assertEqual(push_targets("gitk; echo done"), [])

    def test_unresolvable_cd_is_flagged_not_guessed(self):
        for cmd in (
            'cd "$(git rev-parse --show-toplevel)" && git push origin main',
            "cd $REPO && git push",
            "cd `pwd`/x && git push",
            "cd ../clean; cd -; git push origin main",
            "popd; git push",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(push_target(cmd).cwd, UNRESOLVED_CWD)
        # An absolute cd after a relative one replaces it; ~ expands.
        self.assertEqual(
            push_target("cd a && cd /tmp/x && git push").cwd,
            "/tmp/x" if os.name != "nt" else "/tmp/x",
        )
        self.assertEqual(push_target("cd ~/proj && git push").cwd, os.path.expanduser("~/proj"))
        self.assertEqual(push_target("cd && git push").cwd, os.path.expanduser("~"))

    @unittest.skipUnless(os.name == "nt", "Git Bash drive paths only exist on Windows")
    def test_git_bash_drive_path_is_translated(self):
        self.assertEqual(push_target("cd /d/Professional/x && git push").cwd, "D:/Professional/x")
        self.assertEqual(push_target("cd /c && git push").cwd, "C:/")

    def test_dash_c_sets_cwd(self):
        t = push_target("git -C ../other push origin main")
        self.assertEqual((t.cwd, t.remote, t.refs), ("../other", "origin", ("main",)))
        self.assertEqual(push_target("git -C../other push").cwd, "../other")

    def test_compound_command_finds_the_push_segment(self):
        t = push_target("git add . && git commit -m 'x' && git push origin dev; echo done")
        self.assertEqual((t.remote, t.refs), ("origin", ("dev",)))

    def test_shell_redirections_are_not_remotes_or_refs(self):
        # Regression: ``git push -q 2>&1 | tail`` parsed ``2>&1`` as the remote,
        # ``--remotes=2>&1`` matched nothing, and the whole history was
        # reported as outgoing.
        self.assertEqual(
            push_target("git push -q 2>&1 | tail -2"), PushTarget(None, ("HEAD",), False, None)
        )
        self.assertEqual(push_target("git push origin main 2>/dev/null").remote, "origin")
        self.assertEqual(push_target("git push origin main >push.log").refs, ("main",))
        t = push_target("git push origin main > push.log")
        self.assertEqual((t.remote, t.refs), ("origin", ("main",)))
        t = push_target("git push origin main 2> err.log < /dev/null")
        self.assertEqual((t.remote, t.refs), ("origin", ("main",)))
        self.assertEqual(push_target("git push &> all.log").remote, None)

    def test_cd_before_push_sets_cwd(self):
        # ``cd sub && git push`` runs in sub — the scan must too, or a secret in
        # a nested checkout slips past a scan of the parent repo.
        self.assertEqual(push_target("cd sub && git push origin main").cwd, "sub")
        t = push_target("cd a && cd b && git -C c push")
        self.assertEqual(t.cwd, os.path.join(os.path.join("a", "b"), "c"))
        self.assertEqual(push_target("pushd repo; git push").cwd, "repo")
        self.assertEqual(push_target("cd - && git push").cwd, UNRESOLVED_CWD)

    def test_every_push_segment_is_parsed(self):
        ts = push_targets("git push origin main; git push upstream main")
        self.assertEqual(
            [(t.remote, t.refs) for t in ts], [("origin", ("main",)), ("upstream", ("main",))]
        )
        ts = push_targets("cd x && git push origin a && cd y && git push")
        self.assertEqual(
            [(t.cwd, t.remote) for t in ts], [("x", "origin"), (os.path.join("x", "y"), None)]
        )
        self.assertEqual(push_targets("echo no push here"), [])

    def test_log_args_url_destination_uses_its_tips(self):
        t = PushTarget(None, ("main",), False, None, "/srv/bare")
        args = outgoing_log_args(t, ["main"], ["origin"], ["a" * 40, "b" * 40])
        self.assertEqual(args[-4:], ["main", "--not", "a" * 40, "b" * 40])
        self.assertNotIn("--remotes", " ".join(args))
        # Unknown destination state → scan everything reachable.
        args = outgoing_log_args(t, ["main"], ["origin"], [])
        self.assertEqual(args[-1], "main")
        self.assertNotIn("--not", args)

    def test_log_args_unknown_remote_falls_back_to_all_remotes(self):
        target = PushTarget("2>&1", ("HEAD",), False, None)
        args = outgoing_log_args(target, ["HEAD"], known_remotes=["origin"])
        self.assertEqual(args[-3:], ["HEAD", "--not", "--remotes"])
        args = outgoing_log_args(
            PushTarget("origin", ("HEAD",), False, None), ["HEAD"], known_remotes=["origin"]
        )
        self.assertEqual(args[-1], "--remotes=origin")
        # Without the list the caller gets the old behaviour (pure, no git).
        args = outgoing_log_args(target, ["HEAD"])
        self.assertEqual(args[-1], "--remotes=2>&1")

    def test_log_args(self):
        args = outgoing_log_args(PushTarget("origin", ("main",), False, None), ["main"])
        self.assertEqual(args[-3:], ["main", "--not", "--remotes=origin"])
        self.assertIn("--format=%H", args)
        self.assertIn("-p", args)
        args = outgoing_log_args(PushTarget(None, ("nope",), False, None), [])
        self.assertEqual(args[-3:], ["HEAD", "--not", "--remotes"])
        args = outgoing_log_args(PushTarget("origin", ("HEAD",), True, None), ["HEAD"])
        self.assertEqual(args[-3:], ["--branches", "--not", "--remotes=origin"])


def _git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class OutgoingScanIntegrationTest(unittest.TestCase):
    """Drive the exact git invocation ``_run`` uses against a throwaway repo
    with a bare remote, and prove the per-commit scan catches what the old
    cumulative diff could not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.remote = base / "remote.git"
        self.work = base / "work"
        _git(["init", "-q", "--bare", str(self.remote)], base)
        _git(["init", "-q", "-b", "main", str(self.work)], base)
        for k, v in (
            ("user.email", "t@example.com"),
            ("user.name", "T"),
            ("commit.gpgsign", "false"),
        ):
            _git(["config", k, v], self.work)
        _git(["remote", "add", "origin", str(self.remote)], self.work)
        self._commit("README.md", "hello\n", "init")
        _git(["push", "-q", "origin", "main"], self.work)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, name, content, msg):
        (self.work / name).write_text(content, encoding="utf-8")
        _git(["add", name], self.work)
        _git(["commit", "-q", "-m", msg], self.work)
        return _git(["rev-parse", "HEAD"], self.work).stdout.strip()

    def _outgoing(self):
        target = PushTarget("origin", ("HEAD",), False, None)
        proc = _git(outgoing_log_args(target, ["HEAD"]), self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return scan_patch_series(proc.stdout)

    def test_nothing_outgoing_after_push(self):
        self.assertEqual(self._outgoing(), [])

    def test_secret_added_then_deleted_is_caught_by_per_commit_scan(self):
        bad = self._commit("cfg.py", f"key = '{AWS_ID}'\n", "oops")
        self._commit("cfg.py", "key = None\n", "remove it")
        # The cumulative diff origin/main..HEAD is what the old gate scanned:
        cumulative = _git(["diff", "--no-color", "origin/main", "HEAD"], self.work).stdout
        self.assertEqual(scan_diff(cumulative), [], "cumulative diff hides the leak")
        # The per-commit scan does not.
        findings = self._outgoing()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], bad)
        self.assertEqual(findings[0][1], "cfg.py")

    def test_secret_already_on_remote_is_not_reported(self):
        self._commit("cfg.py", f"key = '{AWS_ID}'\n", "oops")
        _git(["push", "-q", "origin", "main"], self.work)  # already leaked; not *this* push
        self._commit("other.py", "x = 1\n", "clean")
        self.assertEqual(self._outgoing(), [])

    def test_unknown_remote_does_not_widen_the_scan_to_history(self):
        self._commit("cfg.py", f"key = '{AWS_ID}'\n", "oops")
        _git(["push", "-q", "origin", "main"], self.work)
        self._commit("other.py", "x = 1\n", "clean")
        known = _git(["remote"], self.work).stdout.split()
        bad_target = PushTarget("2>&1", ("HEAD",), False, None)
        # The unguarded range reports the already-pushed leak as outgoing…
        proc = _git(outgoing_log_args(bad_target, ["HEAD"]), self.work)
        self.assertEqual(len(scan_patch_series(proc.stdout)), 1)
        # …the guarded one does not.
        proc = _git(outgoing_log_args(bad_target, ["HEAD"], known), self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(scan_patch_series(proc.stdout), [])


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
