#!/usr/bin/env python3
"""Tests for core/hooks/compass_rules.py — the per-turn pin and hook-point rules.

In-process tests drive ``run(payload)`` against a temporary state dir holding a
rendered ``rules.md``; the session flags go to a temporary session-tmp so the
real checkout is never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from compass import rules, store  # noqa: E402
from core import session as core_session  # noqa: E402
from core.hooks import compass_rules, dispatch  # noqa: E402

SID = "abcd1234-1111-2222-3333-444444444444"
NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


class CompassRulesHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.flags = self.root / "session-tmp"
        self.flags.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {store.TARGET_STATE_DIR_ENV: str(self.state), "APIARY_RUNNER_SUBPROCESS": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("APIARY_RUNNER_SUBPROCESS", None)
        flag_patch = mock.patch.object(core_session, "session_tmp_dir", lambda: self.flags)
        flag_patch.start()
        self.addCleanup(flag_patch.stop)
        self.write_rules()

    def write_rules(self, manual_rows=()) -> None:
        text = rules.build(
            seed=store.load_seed_rules(),
            manual_rows=list(manual_rows),
            events=[],
            heuristic_turns=[],
            now=NOW,
        )["text"]
        path = store.rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def prompt(self, **extra) -> dict:
        return {
            "session_id": SID,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.root),
            **extra,
        }

    def tool(self, name: str, **extra) -> dict:
        return {
            "session_id": SID,
            "hook_event_name": "PreToolUse",
            "tool_name": name,
            "cwd": str(self.root),
            **extra,
        }

    # --- the pin ---------------------------------------------------------

    def counter(self) -> Path:
        return self.flags / f"{SID}_{compass_rules.PIN_COUNTER}"

    def messages(self, n: int) -> list:
        """Run the pin hook for *n* consecutive user messages; return its answers."""
        return [compass_rules.run(self.prompt()) for _ in range(n)]

    def test_pins_every_tenth_message_and_the_hook_does_the_counting(self):
        every = compass_rules.PIN_EVERY
        first = self.messages(every - 1)
        self.assertEqual(first, [None] * (every - 1))  # message 1 has the startup block
        self.assertEqual(self.counter().read_text(encoding="utf-8"), str(every - 1))
        tenth = compass_rules.run(self.prompt())
        self.assertIsNotNone(tenth)
        self.assertTrue(tenth.context.startswith("[compass] compass rules pin"))
        self.assertIn("J1 Prefer the thorough option over the quick one.", tenth.context)
        self.assertIn("Self-check before finalizing:", tenth.context)
        self.assertNotIn("J4 ", tenth.context)  # specific rows are not pinned
        self.assertEqual(self.messages(every - 1), [None] * (every - 1))
        twentieth = compass_rules.run(self.prompt())
        self.assertEqual(twentieth.context, tenth.context)
        self.assertEqual(self.counter().read_text(encoding="utf-8"), str(2 * every))

    def test_counter_recovers_from_a_garbage_file(self):
        self.counter().write_text("not a number", encoding="utf-8")
        self.assertIsNone(compass_rules.run(self.prompt()))
        self.assertEqual(self.counter().read_text(encoding="utf-8"), "1")
        self.assertEqual(compass_rules.bump_counter(self.root / "missing" / "count"), 1)

    def test_pin_reads_the_rendered_table_not_the_seed(self):
        self.write_rules(
            manual_rows=[
                {
                    "id": "J1",
                    "section": "judgment",
                    "kind": "principle",
                    "parent": None,
                    "rule": "Prefer thorough (edited).",
                    "why": "w",
                }
            ]
        )
        self.messages(compass_rules.PIN_EVERY - 1)
        self.assertIn("J1 Prefer thorough (edited).", compass_rules.run(self.prompt()).context)

    def test_no_rules_md_means_no_pin(self):
        store.rules_path().unlink()
        self.assertEqual(self.messages(2 * compass_rules.PIN_EVERY), [None] * 20)

    def test_bad_or_missing_session_id_is_a_no_op(self):
        self.assertIsNone(compass_rules.run(self.prompt(session_id="")))
        self.assertIsNone(compass_rules.run(self.prompt(session_id="../../etc")))

    def test_runner_subprocess_is_skipped(self):
        compass_rules.run(self.prompt())
        with mock.patch.dict(os.environ, {"APIARY_RUNNER_SUBPROCESS": "1"}):
            self.assertIsNone(compass_rules.run(self.prompt()))
            self.assertIsNone(compass_rules.run(self.tool("Agent")))

    def test_other_events_without_a_tool_name_are_ignored(self):
        self.assertIsNone(compass_rules.run({"session_id": SID, "hook_event_name": "Stop"}))

    # --- hook-point rules -------------------------------------------------

    def test_agent_spawn_gets_j5_once_per_session_and_agent(self):
        first = compass_rules.run(self.tool("Agent"))
        self.assertIsNotNone(first)
        self.assertTrue(first.context.startswith("[compass] rule before Agent: J5 "))
        self.assertIn("usage-limit draw-down", first.context)
        self.assertIn("(why:", first.context)
        self.assertIsNone(compass_rules.run(self.tool("Agent")))
        self.assertIsNone(compass_rules.run(self.tool("Task")))  # same rule, same flag
        worker = compass_rules.run(self.tool("Agent", agent_id="agent-0001"))
        self.assertIsNotNone(worker)
        self.assertIsNone(compass_rules.run(self.tool("Agent", agent_id="agent-0001")))

    def test_ask_user_question_gets_o3_every_time(self):
        first = compass_rules.run(self.tool("AskUserQuestion"))
        self.assertIn("rule before AskUserQuestion: O3 Ask questions in plain prose", first.context)
        self.assertEqual(compass_rules.run(self.tool("AskUserQuestion")).context, first.context)

    def test_unmapped_tool_or_dropped_rule_injects_nothing(self):
        self.assertIsNone(compass_rules.run(self.tool("Bash")))
        text = store.rules_path().read_text(encoding="utf-8")
        text = "\n".join(ln for ln in text.splitlines() if "**J5**" not in ln) + "\n"
        store.rules_path().write_text(text, encoding="utf-8")
        self.assertIsNone(compass_rules.run(self.tool("Agent")))

    # --- registration -----------------------------------------------------

    def test_registered_for_both_events(self):
        registry = dispatch._registry()
        prompt_names = [h.name for h in registry["UserPromptSubmit"]]
        self.assertEqual(prompt_names, ["startup_prompt", "compass_rules"])
        pre = {h.name: h for h in registry["PreToolUse"]}
        self.assertEqual(pre["compass_rules"].matcher, compass_rules.MATCHER)
        for tool in ("Agent", "Task", "AskUserQuestion"):
            self.assertTrue(dispatch.matches(pre["compass_rules"].matcher, tool))
        self.assertFalse(dispatch.matches(pre["compass_rules"].matcher, "Bash"))

    def test_dispatch_prompt_chain_carries_the_pin(self):
        chain = dispatch._registry()["UserPromptSubmit"]
        only_pin = tuple(h for h in chain if h.name == "compass_rules")
        for _ in range(compass_rules.PIN_EVERY - 1):
            self.assertIsNone(
                dispatch.dispatch("UserPromptSubmit", self.prompt(), only_pin).context
            )
        result = dispatch.dispatch("UserPromptSubmit", self.prompt(), only_pin)
        self.assertIn("compass rules pin", result.context)


if __name__ == "__main__":
    unittest.main()
