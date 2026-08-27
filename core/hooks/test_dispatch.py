#!/usr/bin/env python3
"""Tests for core/hooks/dispatch.py — the one-process-per-event hook runner.

The properties that matter, in the order the review (X-1) asked for them:
order, fail-open isolation, context merging, gate exit 2, matcher gating.
Plus the two things a refactor this wide can quietly break: every registered
module still has a ``run``, and the dispatcher never emits a permission vote.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.hook_context import HookResult  # noqa: E402
from core.hooks import dispatch  # noqa: E402
from core.testing import hermetic_env  # noqa: E402


class _FakeHooks:
    """Build a hook tuple whose modules resolve to in-test callables."""

    def __init__(self):
        self.calls: list[str] = []
        self._fns: dict[str, object] = {}

    def add(self, name, fn, matcher=None):
        self._fns[name] = fn
        return dispatch.Hook(name, f"fake:{name}", matcher)

    def loader(self, module: str):
        name = module.split(":", 1)[1]
        fn = self._fns[name]

        def _run(payload):
            self.calls.append(name)
            return fn(payload)

        return _run

    def record(self, name, result=None, matcher=None):
        """A hook that records it ran and returns *result*."""
        return self.add(name, lambda _payload: result, matcher)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeHooks()
        patcher = mock.patch.object(dispatch, "load_run", self.fake.loader)
        patcher.start()
        self.addCleanup(patcher.stop)
        # hooks.log must never land in this checkout during tests.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        log = Path(self._tmp.name) / ".claude" / "apiary" / "hooks.log"
        log_patch = mock.patch.object(dispatch, "log_path", lambda: log)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.log = log

    # --- order -------------------------------------------------------------

    def test_hooks_run_in_registry_order(self):
        hooks = (self.fake.record("a"), self.fake.record("b"), self.fake.record("c"))
        dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertEqual(self.fake.calls, ["a", "b", "c"])

    def test_real_registry_puts_the_drift_check_first(self):
        pre = dispatch._registry()["PreToolUse"]
        self.assertEqual(pre[0].name, "drift_check")
        # ...and the order the docs claim, core -> budgeter -> docs.
        self.assertEqual(
            [h.name for h in pre],
            ["drift_check", "inject_session", "learnings_inject",
             "research_reminder", "pre_push_doc_conformer", "pre_push_secret_scan",
             "budgeter_pre", "remind_standards"],
        )

    # --- fail-open isolation ----------------------------------------------

    def test_one_hook_raising_does_not_stop_the_others(self):
        def boom(_payload):
            raise RuntimeError("hook is broken")

        hooks = (
            self.fake.record("first", HookResult(context="[a] one")),
            self.fake.add("broken", boom),
            self.fake.record("last", HookResult(context="[b] two")),
        )
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertEqual(self.fake.calls, ["first", "broken", "last"])
        self.assertEqual(result.context, "[a] one\n\n[b] two")
        self.assertIsNone(result.block_reason)

    def test_a_hook_failure_is_logged_not_swallowed(self):
        def boom(_payload):
            raise ValueError("kaboom")

        dispatch.dispatch("PreToolUse", {"tool_name": "Bash"},
                          (self.fake.add("broken", boom),))
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("PreToolUse broken", text)
        self.assertIn("kaboom", text)
        self.assertIn("Traceback", text)

    def test_a_hook_calling_sys_exit_2_is_a_block(self):
        # A hook that still calls sys.exit(2) meant "block"; the dispatcher
        # keeps that meaning instead of dying with no JSON on stdout.
        def exits(_payload):
            raise SystemExit(2)

        hooks = (self.fake.add("exiter", exits),
                 self.fake.record("after", HookResult(context="[b] two")))
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertIn("exited 2", result.block_reason)

    def test_a_hook_calling_sys_exit_0_is_logged_and_the_chain_continues(self):
        # sys.exit(0) from a hook is a failure of that hook alone: logged, the
        # rest of the chain runs, the merged context survives.
        def exits(_payload):
            raise SystemExit(0)

        hooks = (self.fake.add("exiter", exits),
                 self.fake.record("after", HookResult(context="[b] two")))
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertIsNone(result.block_reason)
        self.assertEqual(result.context, "[b] two")
        self.assertIn("exiter", self.log.read_text(encoding="utf-8"))

    def test_a_hook_returning_the_wrong_type_is_logged_not_fatal(self):
        hooks = (self.fake.record("legacy", {"decision": "block"}),
                 self.fake.record("after", HookResult(context="[b] two")))
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertEqual(result.context, "[b] two")
        self.assertIn("expected HookResult", self.log.read_text(encoding="utf-8"))

    def test_log_rotates_at_one_mebibyte(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("x" * (dispatch.LOG_MAX_BYTES + 1), encoding="utf-8")
        dispatch.log_failure("PreToolUse", "h", RuntimeError("fresh"))
        self.assertTrue(self.log.with_suffix(".log.1").exists())
        self.assertIn("fresh", self.log.read_text(encoding="utf-8"))
        self.assertLess(self.log.stat().st_size, dispatch.LOG_MAX_BYTES)

    def test_log_failure_never_raises_when_the_path_is_unwritable(self):
        with mock.patch.object(dispatch, "log_path",
                               side_effect=OSError("no disk")):
            dispatch.log_failure("PreToolUse", "h", RuntimeError("x"))  # no raise

    # --- context merging ---------------------------------------------------

    def test_contexts_merge_into_one_block(self):
        hooks = (
            self.fake.record("a", HookResult(context="[a] one")),
            self.fake.record("b", None),
            self.fake.record("c", HookResult(context="[c] three")),
        )
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertEqual(result.context, "[a] one\n\n[c] three")

    def test_all_silent_hooks_produce_no_context(self):
        hooks = (self.fake.record("a"), self.fake.record("b"))
        self.assertIsNone(
            dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks).context)

    def test_main_prints_one_merged_json_object(self):
        hooks = (self.fake.record("a", HookResult(context="[a] one")),
                 self.fake.record("b", HookResult(context="[b] two")))
        out = self._main("pre", {"tool_name": "Bash"}, hooks)
        self.assertEqual(len(out.splitlines()), 1)
        spec = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertEqual(spec["additionalContext"], "[a] one\n\n[b] two")
        self.assertNotIn("permissionDecision", spec)

    def test_main_prints_bare_object_when_nothing_has_an_opinion(self):
        out = self._main("pre", {"tool_name": "Read"}, (self.fake.record("a"),))
        self.assertEqual(json.loads(out), {})

    # --- gate: block -> exit 2 --------------------------------------------

    def test_a_block_reason_short_circuits_the_chain(self):
        hooks = (
            self.fake.record("a", HookResult(context="[a] one")),
            self.fake.record("gate", HookResult(block_reason="no pushes today")),
            self.fake.record("never", HookResult(context="[c] three")),
        )
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Bash"}, hooks)
        self.assertEqual(result.block_reason, "no pushes today")
        self.assertIsNone(result.context)
        self.assertEqual(self.fake.calls, ["a", "gate"])

    def test_main_exits_2_and_denies_on_a_block(self):
        hooks = (self.fake.record("gate", HookResult(block_reason="secret found")),)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(dispatch, "_registry",
                                  lambda: {"PreToolUse": hooks}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"}))):
            with self.assertRaises(SystemExit) as cm:
                dispatch.main(["pre"])
        self.assertEqual(cm.exception.code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecisionReason"],
                         "secret found")
        self.assertIn("secret found", err.getvalue())

    # --- matcher gating ----------------------------------------------------

    def test_a_non_matching_hook_is_never_run(self):
        hooks = (self.fake.record("bash_only", HookResult(context="[x] y"), "Bash"),
                 self.fake.record("always", HookResult(context="[z] w")))
        result = dispatch.dispatch("PreToolUse", {"tool_name": "Read"}, hooks)
        self.assertEqual(self.fake.calls, ["always"])
        self.assertEqual(result.context, "[z] w")

    def test_a_non_matching_hook_is_not_even_imported(self):
        with mock.patch.object(dispatch, "load_run",
                               side_effect=AssertionError("imported!")) as loader:
            dispatch.dispatch("PreToolUse", {"tool_name": "Read"},
                              (dispatch.Hook("x", "core.does.not.exist", "Bash"),))
            loader.assert_not_called()

    def test_matcher_semantics(self):
        for matcher, tool, expected in [
            ("", "Bash", True), (None, "Bash", True), ("*", "Bash", True),
            ("Bash", "Bash", True), ("Bash", "BashOutput", False),
            ("Edit|Write|Bash", "Write", True), ("Edit|Write|Bash", "Read", False),
            ("WebSearch|WebFetch|Agent|Task", "Task", True),
            ("Bash", "", False), ("", "", True),
            ("[unclosed", "[unclosed", True),   # bad regex -> equality
            ("[unclosed", "Bash", False),
        ]:
            with self.subTest(matcher=matcher, tool=tool):
                self.assertIs(dispatch.matches(matcher, tool), expected)

    def test_matchers_are_gated_on_tool_name_only_for_tool_events(self):
        # Stop / UserPromptSubmit payloads carry no tool_name; a matcher-less
        # hook must still run.
        hooks = (self.fake.record("stopper", HookResult(context="[s] x")),)
        result = dispatch.dispatch("Stop", {"session_id": "abc"}, hooks)
        self.assertEqual(result.context, "[s] x")

    # --- payload handling --------------------------------------------------

    def test_unparseable_payload_is_a_no_opinion_exit_0(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("not json")):
            code = dispatch.main(["pre"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {})
        self.assertIn("<payload>", self.log.read_text(encoding="utf-8"))

    def test_non_dict_payload_is_a_no_opinion_exit_0(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("[1, 2]")):
            code = dispatch.main(["pre"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_bad_verb_exits_2_without_reading_stdin(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(dispatch.main(["nope"]), 2)
            self.assertEqual(dispatch.main([]), 2)
        self.assertIn("usage:", err.getvalue())

    def _main(self, verb, payload, hooks):
        out = io.StringIO()
        event = dispatch.EVENTS[verb]
        with redirect_stdout(out), \
                mock.patch.object(dispatch, "_registry", lambda: {event: hooks}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            dispatch.main([verb])
        return out.getvalue()


class RegistryIntegrityTest(unittest.TestCase):
    """The registry names real modules, and each exposes the run() contract."""

    def test_every_registered_module_has_a_run(self):
        for event, hooks in dispatch._registry().items():
            for hook in hooks:
                with self.subTest(event=event, hook=hook.name):
                    fn = dispatch.load_run(hook.module)
                    self.assertTrue(callable(fn))

    def test_every_hook_script_still_runs_standalone(self):
        """`python <hook>.py` must keep working mid-migration."""
        scripts = [
            REPO / (h.module if h.module.endswith(".py")
                    else h.module.replace(".", "/") + ".py")
            for hooks in dispatch._registry().values() for h in hooks
        ]
        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.is_file(), script)
                src = script.read_text(encoding="utf-8")
                self.assertIn("run_standalone(run", src)

    def test_dispatcher_events_match_the_factory(self):
        from core import hooks_factory
        for event, verb in hooks_factory.EVENT_VERBS.items():
            self.assertEqual(dispatch.EVENTS[verb], event)
        # Every event the factory registers must have a chain to run.
        for event in hooks_factory.EVENT_VERBS:
            self.assertIn(event, dispatch._registry())

    def test_budgeter_matcher_mirrors_its_config(self):
        cfg = json.loads((REPO / "budgeter" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(dispatch.budgeter_matcher(),
                         "|".join(cfg["monitored_tools"]))

    def test_the_duplicate_helper_seam_is_still_marked(self):
        # §5a-C(2): the next hook lands in the pre chain, and the marker is
        # how the next agent finds the spot.
        src = (REPO / "core" / "hooks" / "dispatch.py").read_text(encoding="utf-8")
        self.assertIn("duplicate-helper hook goes here", src)


class EndToEndTest(unittest.TestCase):
    """Run the real dispatcher as a subprocess against a real payload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.repo = self.home / "repo"
        (self.repo / ".claude" / "apiary" / "session-tmp").mkdir(parents=True)
        (self.repo / ".claude" / "apiary" / "self-pointer.json").write_text(
            json.dumps({"schema_version": 1, "name": "repo", "uid": 1,
                        "real_path": str(self.repo)}), encoding="utf-8")

    def _run(self, verb, payload):
        env = hermetic_env(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            APIARY_TARGET_REPO=str(self.repo),
            APIARY_TARGET_STATE_DIR=str(self.home / "state"),
        )
        return subprocess.run(
            [sys.executable, str(REPO / "core" / "hooks" / "dispatch.py"), verb],
            input=json.dumps(payload), text=True, capture_output=True,
            env=env, cwd=str(self.repo), timeout=120,
        )

    def test_pre_on_a_read_call_emits_valid_json_and_exits_0(self):
        r = self._run("pre", {"tool_name": "Read",
                              "tool_input": {"file_path": "x.txt"},
                              "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
                              "cwd": str(self.repo), "transcript_path": ""})
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn("permissionDecision", r.stdout)
        # inject_session fires on the first call of a session.
        self.assertIn("session_id", out["hookSpecificOutput"]["additionalContext"])

    def test_the_session_guard_makes_the_second_call_silent(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "x.txt"},
                   "session_id": "bbbbbbbb-1111-2222-3333-444444444444",
                   "cwd": str(self.repo), "transcript_path": ""}
        self._run("pre", payload)
        r = self._run("pre", payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(json.loads(r.stdout), {})

    def test_post_on_a_failed_bash_call_injects_the_reminder(self):
        r = self._run("post", {"tool_name": "Bash",
                               "tool_response": {"is_error": True, "stderr": "boom"},
                               "session_id": "cccccccc-1111-2222-3333-444444444444"})
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        spec = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PostToolUse")
        self.assertIn("recover_from_trivial_errors", spec["additionalContext"])

    def test_post_on_a_successful_bash_call_says_nothing(self):
        r = self._run("post", {"tool_name": "Bash",
                               "tool_response": {"stdout": "ok", "exit_code": 0},
                               "session_id": "dddddddd-1111-2222-3333-444444444444"})
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(json.loads(r.stdout), {})


if __name__ == "__main__":
    unittest.main()
