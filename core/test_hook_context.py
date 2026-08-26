#!/usr/bin/env python3
"""Unit tests for core.hook_context — the hook response helpers.

The one property that matters most: a hook that merely wants to add context
(or has nothing to say) must NOT emit ``permissionDecision``. Until 2026-08
``hook_allow`` printed ``permissionDecision: "allow"`` on every call, which
auto-approved every tool call and silently disabled default-mode prompts in
every bootstrapped repo (review C-1).
"""
import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from core.hook_context import (
    PERMISSION_DECISIONS,
    context_block,
    hook_allow,
    hook_block,
    join_contexts,
    read_payload,
)

REPO = Path(__file__).resolve().parent.parent


def _capture(fn, *args, **kwargs):
    """Run *fn*, return (stdout, stderr, SystemExit code or None)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*args, **kwargs)
        except SystemExit as e:
            code = e.code
    return out.getvalue(), err.getvalue(), code


class HookAllowTest(unittest.TestCase):
    def test_no_context_prints_empty_object_and_returns(self):
        stdout, _, code = _capture(hook_allow)
        self.assertIsNone(code, "hook_allow must return, not exit")
        self.assertEqual(json.loads(stdout), {})

    def test_never_votes_allow(self):
        for kwargs in ({}, {"context": "[x] hi"}, {"event": "UserPromptSubmit"},
                       {"context": "[x] hi", "event": "PostToolUse"}):
            with self.subTest(kwargs=kwargs):
                stdout, _, _ = _capture(hook_allow, **kwargs)
                self.assertNotIn("permissionDecision", stdout)

    def test_context_is_attached_under_hook_specific_output(self):
        stdout, _, _ = _capture(hook_allow, "[session] session_id: abc")
        self.assertEqual(json.loads(stdout), {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "[session] session_id: abc",
            }
        })

    def test_event_name_is_carried(self):
        stdout, _, _ = _capture(hook_allow, "[startup] x", event="UserPromptSubmit")
        spec = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "UserPromptSubmit")

    def test_empty_context_is_treated_as_no_context(self):
        stdout, _, _ = _capture(hook_allow, "")
        self.assertEqual(json.loads(stdout), {})

    def test_explicit_decision_is_emitted_only_when_asked_for(self):
        for decision in PERMISSION_DECISIONS:
            with self.subTest(decision=decision):
                stdout, _, _ = _capture(hook_allow, decision=decision)
                spec = json.loads(stdout)["hookSpecificOutput"]
                self.assertEqual(spec["permissionDecision"], decision)
                self.assertNotIn("additionalContext", spec)

    def test_unknown_decision_raises_instead_of_voting(self):
        # "block" / "approve" are legacy top-level values, not permissionDecision
        # values; a typo must never become a silent vote.
        for bad in ("block", "approve", "ALLOW", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _capture(hook_allow, decision=bad)

    def test_output_is_a_single_json_line(self):
        stdout, _, _ = _capture(hook_allow, "[a] line one\nline two")
        self.assertEqual(len(stdout.splitlines()), 1)
        json.loads(stdout)


class HookBlockTest(unittest.TestCase):
    def test_denies_with_reason_and_exits_2(self):
        stdout, stderr, code = _capture(hook_block, "no secrets in pushes")
        self.assertEqual(code, 2)
        out = json.loads(stdout)
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertEqual(spec["permissionDecisionReason"], "no secrets in pushes")
        # Legacy pair for older clients.
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["reason"], "no secrets in pushes")
        # Exit-2 path feeds stderr back to Claude as the reason.
        self.assertIn("no secrets in pushes", stderr)

    def test_never_uses_a_non_vocabulary_permission_decision(self):
        stdout, _, _ = _capture(hook_block, "x")
        decision = json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
        self.assertIn(decision, PERMISSION_DECISIONS)


class ReadPayloadTest(unittest.TestCase):
    def test_returns_parsed_dict(self):
        with mock.patch("sys.stdin", io.StringIO('{"tool_name": "Bash"}')):
            self.assertEqual(read_payload(), {"tool_name": "Bash"})

    def test_malformed_input_fails_open_without_a_vote(self):
        with mock.patch("sys.stdin", io.StringIO("not json")):
            stdout, _, code = _capture(read_payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {})


class ContextFormattingTest(unittest.TestCase):
    def test_context_block_tags_first_line_only(self):
        self.assertEqual(context_block("s", "a", "b"), "[s] a\nb")

    def test_join_contexts_drops_empties(self):
        self.assertEqual(join_contexts("[a] 1", "", None, "[b] 2"), "[a] 1\n\n[b] 2")


class NoHookVotesAllowTest(unittest.TestCase):
    """Guard: no hook module may hand-roll an allow/approve vote."""

    VOTE = re.compile(r'permissionDecision["\']?\s*[:=]\s*["\'](allow|approve)["\']')

    def test_no_hook_source_contains_an_allow_vote(self):
        offenders = []
        for path in REPO.glob("*/hooks/*.py"):
            if path.name.startswith("test_"):
                continue
            if self.VOTE.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "hooks must not vote allow — use permissions.allow rules")

    def test_hook_context_itself_has_no_literal_allow_vote(self):
        src = (REPO / "core" / "hook_context.py").read_text(encoding="utf-8")
        self.assertIsNone(self.VOTE.search(src))


if __name__ == "__main__":
    unittest.main()
