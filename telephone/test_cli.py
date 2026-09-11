#!/usr/bin/env python3
"""End-to-end tests for telephone/cli.py against a fake claude binary.

The harness follows ``runner/test_e2e_pipeline.py::install_fake_claude``: a
Python stand-in plus a platform-correct shim on PATH, driven by a JSON script
the test writes. This fake also records its own cwd and environment, can sleep
past the wall-clock limit, can report ``is_error``, and can perform the git work
an act-mode callee would do, so every acceptance criterion that does not need a
real model is covered here.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.session import SessionId
from core.testing import init_git_repo
from telephone import cli, store
from telephone.hooks import user_prompt

SESSION = "aabbccdd-1111-2222-3333-444455556666"

_FAKE_CLAUDE_SRC = r'''#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by telephone/test_cli.py.

Reads the prompt on stdin, logs its cwd / argv / environment, then behaves the
way the test's JSON script says: reply, sleep past the limit, report an error,
or do an act-mode callee's git work.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def envelope(text, session_id, is_error=False, subtype="success"):
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "session_id": session_id,
        "num_turns": 2,
        "duration_ms": 12,
        "total_cost_usd": 0.0012,
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 30,
        },
    })


def streaming():
    return "stream-json" in sys.argv[1:]


def assistant_event(text, session_id):
    return json.dumps({
        "type": "assistant",
        "session_id": session_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def emit(text, session_id, is_error=False, subtype="success", envelope_too=True):
    """Write the reply the way the requested output format would.

    `json` is one envelope. `stream-json` is a system line, one assistant
    event per turn, then the envelope, each on its own line, flushed as it
    goes, which is what lets a killed run leave text behind.
    """
    if not streaming():
        if envelope_too:
            sys.stdout.write(envelope(text, session_id, is_error, subtype))
        else:
            sys.stdout.write(text)
        sys.stdout.flush()
        return
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n")
    if text:
        sys.stdout.write(assistant_event(text, session_id) + "\n")
    if envelope_too:
        sys.stdout.write(envelope(text, session_id, is_error, subtype) + "\n")
    sys.stdout.flush()


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=os.getcwd(), capture_output=True, text=True, encoding="utf-8"
    )


def do_act(spec):
    """What an act-mode callee does. The CLI has already put the checkout on
    the work branch, so the fake only edits and commits where it stands."""
    work_branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    # Unique content every time, so a follow-up on the same branch has
    # something to commit rather than hitting "nothing to commit".
    Path("telephone_work.txt").write_text(f"done {time.time_ns()}\n", encoding="utf-8")
    if spec.get("dirty"):
        return
    git("add", "telephone_work.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
    work_head = git("rev-parse", "HEAD").stdout.strip()
    if spec.get("push"):
        git("update-ref", spec.get("push_ref", "refs/remotes/origin/master"), work_head)
    back_to = spec.get("switch_back")
    if back_to:
        git("checkout", back_to)
    if spec.get("delete_branch"):
        git("checkout", spec.get("delete_branch"))
        git("branch", "-D", work_branch)


def main():
    prompt = sys.stdin.read()
    script = json.loads(
        Path(os.environ["APIARY_FAKE_TELEPHONE_SCRIPT"]).read_text(encoding="utf-8")
    )
    log = os.environ.get("APIARY_FAKE_TELEPHONE_LOG")
    argv = sys.argv[1:]
    if log:
        row = {
            "cwd": os.getcwd(),
            "argv": argv,
            "prompt": prompt,
            "env_keys": sorted(os.environ),
            "telephone_call": os.environ.get("APIARY_TELEPHONE_CALL", ""),
            "branch_at_start": git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        }
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    resumed = "--resume" in argv
    if resumed and script.get("resume_fails"):
        sys.stdout.write("")
        sys.stderr.write("fake claude: no conversation found to resume\n")
        return 3

    mode = script.get("mode", "reply")
    session_id = script.get("session_id", "99999999-8888-7777-6666-555544443333")

    if mode == "sleep":
        emit(script.get("partial", "partial text before the limit"), session_id, envelope_too=False)
        time.sleep(float(script.get("sleep", 3)))
        return 1

    if mode == "error":
        emit(script.get("reply", ""), session_id, is_error=True, subtype="error_max_turns")
        return 1

    if mode == "act":
        do_act(script.get("act", {}))

    reply = script.get("resume_reply") if resumed and script.get("resume_reply") else None
    if reply is None:
        reply = script.get("reply", "## Answer\nok\n\n## Questions for the caller\nnone\n\n## Changes made\nnone\n")
    emit(reply, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def install_fake_claude(bin_dir: Path) -> Path:
    """Write the fake CLI plus a ``claude`` shim into *bin_dir*.

    Same platform rule as the runner's harness: on Windows the shim has to be
    a ``.bat``, because ``CreateProcess`` only ever appends ``.exe`` to a bare
    name and an extensionless script is invisible there.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE_SRC, encoding="utf-8")
    if os.name == "nt":
        shim = bin_dir / "claude.bat"
        shim.write_text(
            '@echo off\r\n"' + sys.executable + '" "' + str(script) + '" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = bin_dir / "claude"
        shim.write_text(
            "#!/bin/sh\nexec " + sys.executable + ' "' + str(script) + '" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return shim


STRUCTURED_REPLY = (
    "## Answer\nThe installer copies commands from every tool dir.\n\n"
    "## Questions for the caller\nnone\n\n"
    "## Changes made\nnone\n"
)


class TelephoneCliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

        self.apiary = self.root / "apiary"
        (self.apiary / ".repos").mkdir(parents=True)
        self.caller = init_git_repo(self.root / "caller").resolve()
        self.callee = init_git_repo(self.root / "callee").resolve()
        self.write_registry({"caller": self.caller, "callee": self.callee})

        self.flags = self.root / "session-tmp"
        self.flags.mkdir()
        self.shim = install_fake_claude(self.root / "bin")
        self.script_path = self.root / "script.json"
        self.log_path = self.root / "fake.log"
        self.set_script(mode="reply", reply=STRUCTURED_REPLY)

        self.base_env = {
            "APIARY_CLAUDE_BIN": str(self.shim),
            "APIARY_FAKE_TELEPHONE_SCRIPT": str(self.script_path),
            "APIARY_FAKE_TELEPHONE_LOG": str(self.log_path),
            "APIARY_MAIN_REPO": str(self.apiary),
            "CLAUDE_PROJECT_DIR": str(self.caller),
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
        }

    # --- fixtures ---------------------------------------------------------

    def write_registry(self, repos: dict):
        payload = {
            str(i): {
                "name": name,
                "real_path": str(path),
                "uid": i,
                "version": "0.1.0",
                "registered_at": "2026-01-01T00:00:00Z",
                "last_used": "2026-01-01T00:00:00Z",
                "verified_ok": True,
            }
            for i, (name, path) in enumerate(repos.items(), start=1)
        }
        (self.apiary / ".repos" / "registry.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def set_script(self, **script):
        self.script_path.write_text(json.dumps(script), encoding="utf-8")

    def grant(self, act: bool = False, repo: str = "callee"):
        with mock.patch("core.session.session_tmp_dir", return_value=self.flags):
            return user_prompt.write_grant(
                SessionId(SESSION), {"act": act, "repo": repo, "message": "x"}
            )

    def run_cli(self, *argv, env: dict | None = None, session: str | None = SESSION):
        out, err = io.StringIO(), io.StringIO()
        environ = {**self.base_env, **(env or {})}
        full = [*argv, "--apiary-repo", str(self.apiary)]
        if session:
            full.extend(["--session-id", session])
        with (
            mock.patch.dict(os.environ, environ, clear=False),
            mock.patch.object(cli, "session_tmp_dir", return_value=self.flags),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(full)
        return code, out.getvalue(), err.getvalue()

    def calls_made(self) -> list[dict]:
        if not self.log_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def only_record(self):
        rows = store.list_records(self.apiary)
        self.assertEqual(len(rows), 1, rows)
        return store.load(rows[0]["id"], self.apiary)

    def git(self, repo: Path, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


class TestAnswerCall(TelephoneCliTestCase):
    def test_a_user_typed_call_records_the_exchange_and_prints_the_reply(self):
        self.grant()
        code, out, err = self.run_cli("call", "callee", "what does the installer copy")
        self.assertEqual(code, 0, err)

        path, meta, body = self.only_record()
        self.assertEqual(meta["mode"], "answer")
        self.assertEqual(meta["initiated_by"], "user")
        self.assertEqual(meta["status"], store.STATUS_ANSWERED)
        self.assertEqual(meta["caller"], "caller")
        self.assertEqual(meta["callee"], "callee")
        self.assertEqual(meta["exchanges"], "1")
        exchange = store.parse_exchanges(body)[0]
        self.assertEqual(exchange["message"], "what does the installer copy")
        self.assertIn("The installer copies commands", exchange["reply"])

        self.assertIn("The installer copies commands", out)
        self.assertIn(meta["id"], out)
        self.assertIn("initiated_by=user", out)

    def test_the_callee_runs_in_its_own_checkout(self):
        self.grant()
        self.run_cli("call", "callee", "hello")
        made = self.calls_made()
        self.assertEqual(len(made), 1)
        self.assertEqual(Path(made[0]["cwd"]).resolve(), self.callee)

    def test_answer_mode_tool_lists_reach_the_callee_argv(self):
        self.grant()
        self.run_cli("call", "callee", "hello")
        argv = self.calls_made()[0]["argv"]
        self.assertIn("--allowedTools", argv)
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Read", argv)
        self.assertIn("Write", argv[argv.index("--disallowedTools") :])
        self.assertNotIn("--permission-mode", argv)
        # Streamed so a killed run still leaves its assistant turns behind;
        # the CLI insists on --verbose for that format in print mode.
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        # Answer mode is read-only: no blanket python, only the scribe shape.
        self.assertNotIn("Bash(python *)", argv)
        self.assertIn("Bash(python * scribe/notes.py *)", argv)

    def test_the_callee_environment_is_scrubbed_and_marked(self):
        self.grant()
        self.run_cli("call", "callee", "hello")
        row = self.calls_made()[0]
        self.assertNotIn("CLAUDECODE", row["env_keys"])
        self.assertFalse([k for k in row["env_keys"] if k.startswith("CLAUDE_CODE_")])
        self.assertIn("APIARY_TELEPHONE_CALL", row["env_keys"])
        self.assertTrue(row["telephone_call"].startswith("C-"))
        for leaked in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_STATE_DIR", "APIARY_MAIN_REPO"):
            self.assertNotIn(leaked, row["env_keys"])

    def test_the_store_and_its_year_folder_are_created_on_the_first_call(self):
        self.assertFalse(store.store_dir(self.apiary).exists())
        self.grant()
        self.run_cli("call", "callee", "hello")
        path, meta, _body = self.only_record()
        self.assertTrue(path.is_file())
        self.assertTrue((path.parent / store.NEXT_SEQ_FILENAME).is_file())

    def test_two_calls_to_the_same_callee_get_distinct_records(self):
        code_a, _out, _err = self.run_cli("call", "callee", "first")
        code_b, _out, _err = self.run_cli("call", "callee", "second")
        self.assertEqual((code_a, code_b), (0, 0))
        rows = store.list_records(self.apiary)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_an_unstructured_reply_still_reaches_the_caller(self):
        self.set_script(mode="reply", reply="Plain prose, no headings.")
        code, out, _err = self.run_cli("call", "callee", "hello")
        self.assertEqual(code, 0)
        self.assertIn("Plain prose, no headings.", out)
        _path, _meta, body = self.only_record()
        self.assertIn("did not use the three headings", body)


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #


class TestReply(TelephoneCliTestCase):
    def _place(self):
        self.grant()
        self.run_cli("call", "callee", "first question")
        return store.list_records(self.apiary)[0]["id"]

    def test_a_follow_up_resumes_the_callee_session_in_the_same_record(self):
        call_id = self._place()
        self.set_script(
            mode="reply",
            reply=STRUCTURED_REPLY,
            resume_reply="## Answer\nSecond answer.\n\n## Questions for the caller\nnone\n\n## Changes made\nnone\n",
        )
        code, out, err = self.run_cli("reply", call_id, "second question")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(store.list_records(self.apiary)), 1)

        argv = self.calls_made()[-1]["argv"]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "99999999-8888-7777-6666-555544443333")

        _path, meta, body = self.only_record()
        self.assertEqual(meta["exchanges"], "2")
        items = store.parse_exchanges(body)
        self.assertEqual(items[1]["message"], "second question")
        self.assertIn("Second answer.", items[1]["reply"])
        self.assertIn("Second answer.", out)

    def test_a_record_with_no_callee_session_id_starts_fresh_and_quotes_the_line(self):
        call_id = self._place()
        path = store.record_path(call_id, self.apiary)
        meta, body = store.read_record(path)
        meta["callee_session_id"] = ""
        store.write_record(path, meta, body)

        code, _out, err = self.run_cli("reply", call_id, "second question")
        self.assertEqual(code, 0, err)
        last = self.calls_made()[-1]
        self.assertNotIn("--resume", last["argv"])
        self.assertIn("first question", last["prompt"])
        self.assertIn("could not be resumed", last["prompt"])

    def test_a_failed_resume_falls_back_to_a_fresh_run_and_is_noted(self):
        call_id = self._place()
        self.set_script(mode="reply", reply=STRUCTURED_REPLY, resume_fails=True)
        code, _out, err = self.run_cli("reply", call_id, "second question")
        self.assertEqual(code, 0, err)
        made = self.calls_made()
        self.assertIn("--resume", made[-2]["argv"])
        self.assertNotIn("--resume", made[-1]["argv"])
        self.assertIn("first question", made[-1]["prompt"])
        _path, meta, body = self.only_record()
        self.assertEqual(meta["exchanges"], "2")
        self.assertIn("resume failed", body)


# --------------------------------------------------------------------------- #
# Act mode
# --------------------------------------------------------------------------- #


class TestActMode(TelephoneCliTestCase):
    """The CLI owns the work branch: it creates it before the callee runs and
    restores the checkout afterwards. The fake callee only edits and commits
    where it finds itself, the way a real one is told to."""

    def setUp(self):
        super().setUp()
        self.started_on = self.git(self.callee, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.next_id = store.format_id(int(store.now_iso()[:4]), 1)
        self.work_branch = f"telephone/{self.next_id}"

    def head_of(self, repo: Path) -> str:
        return self.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def test_an_act_call_with_a_grant_branches_and_comes_back(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})

        code, out, err = self.run_cli("call", "callee", "fix the installer gap", "--act")
        self.assertEqual(code, 0, err)

        _path, meta, body = self.only_record()
        self.assertEqual(meta["id"], self.next_id)
        self.assertEqual(meta["mode"], "act")
        self.assertEqual(meta["branch"], self.work_branch)
        self.assertEqual(meta["initiated_by"], "user")
        self.assertEqual(meta["issue"], "")
        self.assertTrue(cli.branch_exists(self.callee, self.work_branch))
        self.assertEqual(self.head_of(self.callee), self.started_on)
        ahead = self.git(
            self.callee, "rev-list", "--count", f"{self.started_on}..{self.work_branch}"
        ).stdout.strip()
        self.assertEqual(ahead, "1")
        self.assertIn(f"branch={self.work_branch}", out)
        self.assertIn(f"commits on {self.work_branch}: 1", body)
        self.assertIn(f"checkout restored to {self.started_on}", body)

        argv = self.calls_made()[0]["argv"]
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("Write", argv[argv.index("--allowedTools") :])

    def test_the_cli_puts_the_callee_on_the_work_branch_before_it_runs(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})
        self.run_cli("call", "callee", "do the work", "--act")
        self.assertEqual(self.calls_made()[0]["branch_at_start"], self.work_branch)
        # And the preamble no longer asks the callee to create it.
        self.assertIn("already on the work branch", self.calls_made()[0]["prompt"])

    def test_uncommitted_work_stays_on_the_branch_and_is_flagged(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={"dirty": True})
        code, out, err = self.run_cli("call", "callee", "do the work", "--act")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertEqual(meta["issue"], f"uncommitted changes left on {self.work_branch}")
        self.assertIn("issue: uncommitted changes left on", body)
        self.assertIn("uncommitted changes left on", out)
        # Switching would have carried the edits along, so the checkout stays.
        self.assertEqual(self.head_of(self.callee), self.work_branch)
        self.assertTrue((self.callee / "telephone_work.txt").is_file())

    def test_a_callee_that_switched_back_itself_is_noted_not_failed(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={"switch_back": self.started_on})
        code, _out, err = self.run_cli("call", "callee", "do the work", "--act")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertEqual(meta["issue"], "")
        self.assertIn("callee moved the checkout to", body)
        self.assertEqual(self.head_of(self.callee), self.started_on)

    def test_a_moved_remote_tracking_ref_is_recorded_as_a_push(self):
        self.grant(act=True)
        head = self.git(self.callee, "rev-parse", "HEAD").stdout.strip()
        self.git(self.callee, "update-ref", "refs/remotes/origin/master", head)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={"push": True})
        code, out, err = self.run_cli("call", "callee", "do the work", "--act")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertEqual(meta["issue"], "push detected")
        self.assertIn("issue: push detected", body)
        self.assertIn("push detected", out)
        note = cli.note_text(
            call_id=meta["id"],
            caller="caller",
            callee="callee",
            mode="act",
            status="answered",
            initiated_by="user",
            record=Path("x"),
            message="do the work",
            issue=meta["issue"],
        )
        self.assertIn("push detected", note)

    def test_a_deleted_work_branch_is_recorded_as_missing(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={"delete_branch": self.started_on})
        code, _out, err = self.run_cli("call", "callee", "do the work", "--act")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertIn("branch missing", body)
        self.assertIn("work branch missing", meta["issue"])

    def test_an_act_follow_up_returns_to_the_work_branch_and_back(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})
        self.run_cli("call", "callee", "first", "--act")
        self.assertEqual(self.head_of(self.callee), self.started_on)

        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})
        code, _out, err = self.run_cli("reply", self.next_id, "and now this")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls_made()[-1]["branch_at_start"], self.work_branch)
        self.assertEqual(self.head_of(self.callee), self.started_on)
        ahead = self.git(
            self.callee, "rev-list", "--count", f"{self.started_on}..{self.work_branch}"
        ).stdout.strip()
        self.assertEqual(ahead, "2")

    def test_an_act_follow_up_on_a_dirty_tree_is_refused(self):
        self.grant(act=True)
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={"dirty": True})
        self.run_cli("call", "callee", "first", "--act")
        self.grant(act=True)
        code, _out, err = self.run_cli("reply", self.next_id, "and now this")
        self.assertEqual(code, 1)
        self.assertIn("tree is dirty", err)
        self.assertEqual(len(self.calls_made()), 1)


# --------------------------------------------------------------------------- #
# Refusals: every one of them must happen before a callee run starts
# --------------------------------------------------------------------------- #


class TestRefusals(TelephoneCliTestCase):
    def assertNoRunStarted(self):
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(store.list_records(self.apiary), [])

    def test_act_without_a_grant_is_refused(self):
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("act needs the user to type", err)
        self.assertNoRunStarted()

    def test_act_with_an_answer_only_grant_is_refused(self):
        self.grant(act=False)
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("act needs the user to type", err)
        self.assertNoRunStarted()

    def test_act_is_refused_when_the_hook_never_wrote_anything(self):
        # Identical outcome to "no grant": the hook failing open removes a
        # capability instead of granting one.
        self.assertEqual(list(self.flags.iterdir()), [])
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("act needs the user to type", err)
        self.assertNoRunStarted()

    def test_an_unknown_repo_lists_the_registered_names(self):
        code, _out, err = self.run_cli("call", "nonexistent-repo", "hi")
        self.assertEqual(code, 1)
        self.assertIn("no registered repo named 'nonexistent-repo'", err)
        self.assertIn("caller", err)
        self.assertIn("callee", err)
        self.assertNoRunStarted()

    def test_an_ambiguous_name_asks_for_the_path(self):
        self.write_registry({"callee": self.callee})
        registry = json.loads(
            (self.apiary / ".repos" / "registry.json").read_text(encoding="utf-8")
        )
        registry["2"] = {**registry["1"], "uid": 2, "real_path": str(self.caller)}
        (self.apiary / ".repos" / "registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        code, _out, err = self.run_cli("call", "callee", "hi")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        self.assertNoRunStarted()

    def test_a_registered_repo_with_no_checkout_names_the_path(self):
        gone = self.root / "vanished"
        self.write_registry({"caller": self.caller, "callee": gone})
        code, _out, err = self.run_cli("call", "callee", "hi")
        self.assertEqual(code, 1)
        self.assertIn(str(gone), err)
        self.assertNoRunStarted()

    def test_a_self_call_is_refused(self):
        code, _out, err = self.run_cli("call", "caller", "hi")
        self.assertEqual(code, 1)
        self.assertIn("self call", err)
        self.assertNoRunStarted()

    def test_a_nested_call_is_refused(self):
        code, _out, err = self.run_cli(
            "call", "callee", "hi", env={cli.TELEPHONE_ENV_VAR: "C-2026-9"}
        )
        self.assertEqual(code, 1)
        self.assertIn("nested call", err)
        self.assertIn("C-2026-9", err)
        self.assertNoRunStarted()

    def test_a_missing_claude_binary_exits_two(self):
        code, _out, err = self.run_cli(
            "call", "callee", "hi", env={"APIARY_CLAUDE_BIN": str(self.root / "no-such-claude")}
        )
        self.assertEqual(code, cli.EXIT_NO_BINARY)
        self.assertIn("could not launch", err)
        self.assertEqual(self.calls_made(), [])


class TestGrantBinding(TelephoneCliTestCase):
    """A grant is spent only on the repo the user named, only while fresh, and
    only by a call that actually runs."""

    def grants_left(self):
        return list(self.flags.glob(f"*_{user_prompt.GRANT_SUFFIX}"))

    def counter(self):
        with mock.patch.object(cli, "session_tmp_dir", return_value=self.flags):
            return cli.auto_call_count(SESSION[:8])

    def test_a_grant_for_another_repo_does_not_unlock_act(self):
        self.grant(act=True, repo="caller")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("act needs the user to type", err)
        self.assertIn("the grant is for caller", err)
        self.assertEqual(self.calls_made(), [])
        # Left in place: the user may still want the call they typed.
        self.assertEqual(len(self.grants_left()), 1)

    def test_a_grant_for_another_repo_makes_an_answer_call_autonomous(self):
        self.grant(repo="caller")
        code, out, err = self.run_cli("call", "callee", "hello")
        self.assertEqual(code, 0, err)
        self.assertIn("initiated_by=model", out)
        self.assertEqual(self.counter(), 1)
        self.assertEqual(len(self.grants_left()), 1)

    def test_a_grant_typed_as_a_path_binds_to_the_same_checkout(self):
        self.grant(act=True, repo=str(self.callee))
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})
        code, out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 0, err)
        self.assertIn("initiated_by=user", out)

    def test_an_expired_grant_is_ignored_and_removed(self):
        path = self.grant(act=True)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["granted_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(data), encoding="utf-8")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        self.assertEqual(self.grants_left(), [])
        self.assertEqual(self.calls_made(), [])

    def test_a_grant_without_a_timestamp_is_not_trusted(self):
        path = self.grant(act=True)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["granted_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertEqual(self.grants_left(), [])

    def test_a_refused_act_call_keeps_the_grant_for_the_retry(self):
        self.grant(act=True)
        (self.callee / "scratch.txt").write_text("x", encoding="utf-8")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("tree is dirty", err)
        self.assertEqual(len(self.grants_left()), 1)
        (self.callee / "scratch.txt").unlink()
        self.set_script(mode="act", reply=STRUCTURED_REPLY, act={})
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.grants_left(), [])

    def test_a_refused_self_call_keeps_the_grant(self):
        self.grant(repo="caller")
        self.run_cli("call", "caller", "hi")
        self.assertEqual(len(self.grants_left()), 1)

    def test_a_reply_grant_is_bound_to_the_records_callee(self):
        self.grant()
        self.run_cli("call", "callee", "first")
        call_id = store.list_records(self.apiary)[0]["id"]
        path = store.record_path(call_id, self.apiary)
        meta, body = store.read_record(path)
        meta["mode"] = store.MODE_ACT
        meta["branch"] = f"telephone/{call_id}"
        store.write_record(path, meta, body)

        self.grant(act=True, repo="caller")
        code, _out, err = self.run_cli("reply", call_id, "and now this")
        self.assertEqual(code, 1)
        self.assertIn("act follow-ups need the user to type", err)
        self.assertIn("the grant is for caller", err)
        self.assertEqual(len(self.calls_made()), 1)


class TestActPreflight(TelephoneCliTestCase):
    def setUp(self):
        super().setUp()
        self.grant(act=True)

    def test_a_dirty_callee_tree_is_refused(self):
        (self.callee / "scratch.txt").write_text("x", encoding="utf-8")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("tree is dirty", err)
        self.assertEqual(self.calls_made(), [])

    def test_a_callee_on_another_branch_is_refused(self):
        self.git(self.callee, "checkout", "-b", "side-quest")
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn("side-quest", err)
        self.assertIn("not its default branch", err)
        self.assertEqual(self.calls_made(), [])

    def test_a_second_act_call_while_one_is_open_is_refused(self):
        open_id = store.allocate_id(self.apiary)
        store.write_record(
            store.record_path(open_id, self.apiary),
            {
                "id": open_id,
                "mode": store.MODE_ACT,
                "status": store.STATUS_OPEN,
                "callee": "callee",
                "caller": "caller",
            },
            "# head\n",
        )
        code, _out, err = self.run_cli("call", "callee", "fix it", "--act")
        self.assertEqual(code, 1)
        self.assertIn(open_id, err)
        self.assertEqual(self.calls_made(), [])


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


class TestAutonomousCaps(TelephoneCliTestCase):
    def counter(self):
        with mock.patch.object(cli, "session_tmp_dir", return_value=self.flags):
            return cli.auto_call_count(SESSION[:8])

    def test_an_autonomous_call_bumps_the_counter(self):
        self.assertEqual(self.counter(), 0)
        self.run_cli("call", "callee", "hi")
        self.assertEqual(self.counter(), 1)

    def test_a_granted_call_does_not_bump_the_counter(self):
        self.grant()
        code, out, err = self.run_cli("call", "callee", "hi")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.counter(), 0)
        _path, meta, _body = self.only_record()
        self.assertEqual(meta["initiated_by"], "user")
        self.assertIn("initiated_by=user", out)

    def test_the_fourth_autonomous_call_is_refused_and_the_counter_stands(self):
        for _ in range(3):
            self.assertEqual(self.run_cli("call", "callee", "hi")[0], 0)
        self.assertEqual(self.counter(), 3)
        code, _out, err = self.run_cli("call", "callee", "one too many")
        self.assertEqual(code, 1)
        self.assertIn("autonomous call cap reached", err)
        self.assertEqual(self.counter(), 3)
        self.assertEqual(len(self.calls_made()), 3)

    def test_a_grant_still_works_past_the_cap(self):
        for _ in range(3):
            self.run_cli("call", "callee", "hi")
        self.grant()
        self.assertEqual(self.run_cli("call", "callee", "user asked")[0], 0)
        self.assertEqual(self.counter(), 3)

    def test_a_refused_call_never_costs_a_slot(self):
        self.run_cli("call", "nonexistent-repo", "hi")
        self.assertEqual(self.counter(), 0)

    def test_the_session_comes_from_the_claude_code_env_when_no_flag_is_passed(self):
        # The Bash tool exports CLAUDE_CODE_SESSION_ID. That, not the newest
        # identity file on the repo, is how the CLI finds the hook's grant.
        self.grant()
        code, out, err = self.run_cli(
            "call", "callee", "hi", env={cli.SESSION_ENV_VAR: SESSION}, session=None
        )
        self.assertEqual(code, 0, err)
        self.assertIn("initiated_by=user", out)
        self.assertEqual(self.counter(), 0)

    def test_an_explicit_session_id_beats_the_env(self):
        self.grant()
        other = "99999999-0000-0000-0000-000000000000"
        code, out, err = self.run_cli(
            "call", "callee", "hi", env={cli.SESSION_ENV_VAR: SESSION}, session=other
        )
        self.assertEqual(code, 0, err)
        self.assertIn("initiated_by=model", out)

    def test_no_session_at_all_is_autonomous_and_still_capped(self):
        self.grant()
        env = {k: v for k, v in os.environ.items() if k != cli.SESSION_ENV_VAR}
        with mock.patch.dict(os.environ, env, clear=True):
            for _ in range(3):
                code, out, err = self.run_cli("call", "callee", "hi", session=None)
                self.assertEqual(code, 0, err)
                self.assertIn("initiated_by=model", out)
            code, _out, err = self.run_cli("call", "callee", "one more", session=None)
        self.assertEqual(code, 1)
        self.assertIn("autonomous call cap reached", err)
        self.assertTrue(list(self.flags.glob(f"{cli.NO_SESSION_KEY}_*")))
        # The grant the hook wrote for the real session is untouched.
        self.assertEqual(len(list(self.flags.glob(f"*_{user_prompt.GRANT_SUFFIX}"))), 1)


class TestExchangeCap(TelephoneCliTestCase):
    def _record_with(self, exchanges: int, mode: str = store.MODE_ANSWER):
        call_id = store.allocate_id(self.apiary)
        path = store.record_path(call_id, self.apiary)
        body = "# head\n"
        for n in range(1, exchanges + 1):
            body += "\n" + store.render_exchange(
                n, sent=f"q{n}", reply=f"a{n}", status="answered", at="t"
            )
        store.write_record(
            path,
            {
                "id": call_id,
                "mode": mode,
                "status": store.STATUS_ANSWERED,
                "caller": "caller",
                "caller_path": str(self.caller),
                "callee": "callee",
                "callee_path": str(self.callee),
                "callee_session_id": "99999999-8888-7777-6666-555544443333",
                "branch": f"telephone/{call_id}" if mode == store.MODE_ACT else "",
                "exchanges": str(exchanges),
            },
            body,
        )
        return call_id

    def test_the_sixth_autonomous_exchange_is_allowed(self):
        call_id = self._record_with(5)
        code, _out, err = self.run_cli("reply", call_id, "sixth")
        self.assertEqual(code, 0, err)

    def test_the_seventh_autonomous_exchange_is_refused(self):
        call_id = self._record_with(6)
        code, _out, err = self.run_cli("reply", call_id, "seventh")
        self.assertEqual(code, 1)
        self.assertIn("autonomous limit", err)
        self.assertIn("Bring the thread to the user", err)
        self.assertEqual(self.calls_made(), [])

    def test_a_grant_lifts_the_exchange_cap(self):
        call_id = self._record_with(6)
        self.grant()
        code, _out, err = self.run_cli("reply", call_id, "seventh")
        self.assertEqual(code, 0, err)

    def test_an_act_follow_up_without_a_fresh_grant_is_refused(self):
        call_id = self._record_with(1, mode=store.MODE_ACT)
        code, _out, err = self.run_cli("reply", call_id, "and now this")
        self.assertEqual(code, 1)
        self.assertIn("act follow-ups need the user to type", err)
        self.assertEqual(self.calls_made(), [])

    def test_an_act_follow_up_with_a_fresh_act_grant_runs(self):
        call_id = self._record_with(1, mode=store.MODE_ACT)
        self.grant(act=True)
        code, _out, err = self.run_cli("reply", call_id, "and now this")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls_made()), 1)

    def test_a_reply_to_an_unknown_call_is_refused(self):
        code, _out, err = self.run_cli("reply", "C-2026-999", "hello")
        self.assertEqual(code, 1)
        self.assertIn("no telephone record", err)
        code, _out, err = self.run_cli("reply", "not-an-id", "hello")
        self.assertEqual(code, 1)
        self.assertIn("not a call id", err)


# --------------------------------------------------------------------------- #
# Failure modes of the callee run itself
# --------------------------------------------------------------------------- #


class TestCalleeFailures(TelephoneCliTestCase):
    def test_a_timeout_kills_the_run_and_keeps_what_was_written(self):
        self.set_script(mode="sleep", sleep=3, partial="halfway through the answer")
        code, out, err = self.run_cli("call", "callee", "take your time", "--timeout", "1")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertEqual(meta["status"], store.STATUS_TIMED_OUT)
        self.assertIn("timed out", out)
        # The assistant event streamed before the kill is the partial reply,
        # as text, not as the raw event line.
        self.assertIn("halfway through the answer", body)
        self.assertIn("halfway through the answer", out)
        self.assertNotIn('"type": "assistant"', body)

    def test_an_error_envelope_becomes_a_failed_record_with_the_reason(self):
        self.set_script(mode="error", reply="the model gave up")
        code, out, err = self.run_cli("call", "callee", "hello")
        self.assertEqual(code, 0, err)
        _path, meta, body = self.only_record()
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertIn("error_max_turns", out)
        self.assertIn("error_max_turns", body)

    def test_a_grant_is_consumed_even_when_the_callee_fails(self):
        self.set_script(mode="error", reply="nope")
        self.grant()
        self.run_cli("call", "callee", "hello")
        self.assertEqual(list(self.flags.glob(f"*_{user_prompt.GRANT_SUFFIX}")), [])


# --------------------------------------------------------------------------- #
# The read-only verbs
# --------------------------------------------------------------------------- #


class TestReadVerbs(TelephoneCliTestCase):
    def _place(self):
        self.grant()
        self.run_cli("call", "callee", "what does the installer copy")
        return store.list_records(self.apiary)[0]["id"]

    def test_list_and_status_and_show(self):
        call_id = self._place()
        code, out, _err = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn(call_id, out)

        code, out, _err = self.run_cli("status", call_id)
        self.assertEqual(code, 0)
        self.assertIn(call_id, out)
        self.assertIn("answered", out)

        code, out, _err = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("1 call(s) on record", out)
        self.assertIn("autonomous call(s)", out)

        code, out, _err = self.run_cli("show", call_id)
        self.assertEqual(code, 0)
        self.assertIn("what does the installer copy", out)
        self.assertIn("The installer copies commands", out)

    def test_list_filters(self):
        self._place()
        code, out, _err = self.run_cli("list", "--status", "open")
        self.assertEqual(code, 0)
        self.assertIn("no telephone records match", out)
        code, out, _err = self.run_cli("list", "--callee", "callee")
        self.assertIn("callee", out)

    def test_hangup_closes_an_open_record(self):
        call_id = store.allocate_id(self.apiary)
        store.write_record(
            store.record_path(call_id, self.apiary),
            {"id": call_id, "status": store.STATUS_OPEN, "mode": "answer", "callee": "callee"},
            "# head\n",
        )
        code, out, _err = self.run_cli("hangup", call_id)
        self.assertEqual(code, 0)
        self.assertIn("hung_up", out)
        _path, meta, _body = store.load(call_id, self.apiary)
        self.assertEqual(meta["status"], store.STATUS_HUNG_UP)

        code, out, _err = self.run_cli("hangup", call_id)
        self.assertEqual(code, 0)
        self.assertIn("already", out)

    def test_show_on_an_unknown_id_is_refused(self):
        code, _out, err = self.run_cli("show", "C-2026-404")
        self.assertEqual(code, 1)
        self.assertIn("no telephone record", err)


# --------------------------------------------------------------------------- #
# The scribe notes, against real bootstrapped repos
# --------------------------------------------------------------------------- #


class TestScribeNotes(unittest.TestCase):
    """Both sides get a context note, through each repo's own launcher.

    This one pays for a real fake main-apiary and two real installs, because
    the note path *is* the launcher path: ``incubator/cli.py::_run_scribe``
    writes into another repo's store the same way, and a stub would not
    exercise the resolution that makes it land in the right place.
    """

    def setUp(self):
        from core.install import install
        from core.testing import make_fake_apiary

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            self.apiary = make_fake_apiary(
                self.root, git=True, self_bootstrap=True, extra_trees=("core", "scribe", "prose")
            )
            self.caller = init_git_repo(self.root / "caller").resolve()
            self.callee = init_git_repo(self.root / "callee").resolve()
            install(self.caller, apiary_repo=self.apiary)
            install(self.callee, apiary_repo=self.apiary)

        self.flags = self.root / "session-tmp"
        self.flags.mkdir()
        self.shim = install_fake_claude(self.root / "bin")
        self.script_path = self.root / "script.json"
        self.script_path.write_text(
            json.dumps({"mode": "reply", "reply": STRUCTURED_REPLY}), encoding="utf-8"
        )
        self.env = {
            "APIARY_CLAUDE_BIN": str(self.shim),
            "APIARY_FAKE_TELEPHONE_SCRIPT": str(self.script_path),
            "APIARY_MAIN_REPO": str(self.apiary),
            "CLAUDE_PROJECT_DIR": str(self.caller),
        }

    def notes_in(self, repo: Path) -> list[dict]:
        from scribe.api import open_store

        return open_store(repo, apiary_repo=self.apiary).list_notes(note_type="context")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env, clear=False),
            mock.patch.object(cli, "session_tmp_dir", return_value=self.flags),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main([*argv, "--apiary-repo", str(self.apiary), "--session-id", SESSION])
        return code, out.getvalue(), err.getvalue()

    def test_a_completed_call_writes_a_context_note_in_both_repos(self):
        self.assertEqual(self.notes_in(self.caller), [])
        self.assertEqual(self.notes_in(self.callee), [])

        code, out, err = self.run_cli("call", "callee", "what does the installer copy")
        self.assertEqual(code, 0, err)
        call_id = store.list_records(self.apiary)[0]["id"]
        record = store.record_path(call_id, self.apiary)

        from scribe.api import open_store

        for repo in (self.caller, self.callee):
            rows = self.notes_in(repo)
            self.assertEqual(len(rows), 1, f"{repo}: {rows}")
            self.assertIn("telephone", rows[0].get("tags") or [])
            note = open_store(repo, apiary_repo=self.apiary).get_note(
                "context", rows[0]["year"], rows[0]["seq"]
            )
            self.assertIn(call_id, note["content"])
            self.assertIn("answer mode", note["content"])
            self.assertIn(str(record), note["content"])
        self.assertIn("note caller:", out)
        self.assertIn("note callee:", out)

    def test_a_note_the_prose_gate_refuses_is_retried_with_force(self):
        from scribe.api import open_store

        flag = self.callee / ".claude" / "apiary" / "flags" / "prose-gate-enabled"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("enabled", encoding="utf-8")

        tripping = "Telephone call C-2026-1: caller called callee — an em-dash the gate refuses.\n"
        with mock.patch.dict(os.environ, self.env, clear=False):
            outcome = cli.write_note(self.callee, tripping)
        self.assertIn("forced", outcome, outcome)

        rows = open_store(self.callee, apiary_repo=self.apiary).list_notes(note_type="context")
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("prose-forced", rows[0].get("tags") or [])

    def test_a_repo_without_a_launcher_is_skipped_rather_than_failing(self):
        bare = init_git_repo(self.root / "bare").resolve()
        with mock.patch.dict(os.environ, self.env, clear=False):
            outcome = cli.write_note(bare, "anything\n")
        self.assertIn("no launcher", outcome)


if __name__ == "__main__":
    unittest.main()
