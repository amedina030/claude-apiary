"""Tests for compass.turns — the record filter and the incremental pair cursor."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import store, turns  # noqa: E402

SID = "abcd1234-1111-2222-3333-444444444444"


def user(prompt_id: str, text: str, **extra) -> dict:
    record = {
        "type": "user",
        "promptId": prompt_id,
        "timestamp": f"2026-09-06T00:00:{int(prompt_id[-2:]):02d}Z",
        "entrypoint": "cli",
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }
    record.update(extra)
    return record


def asst(*texts: str, **extra) -> dict:
    blocks = [{"type": "text", "text": t} for t in texts]
    blocks.append({"type": "tool_use", "name": "Bash", "input": {"command": "ls"}})
    record = {
        "type": "assistant",
        "timestamp": "2026-09-06T00:00:00Z",
        "entrypoint": "cli",
        "message": {"role": "assistant", "content": blocks},
    }
    record.update(extra)
    return record


class FilterTests(unittest.TestCase):
    def test_plain_prompt_is_kept(self):
        self.assertEqual(turns.user_prompt_text(user("p01", "  do it  ")), "do it")

    def test_records_without_prompt_id_or_with_attachment_are_dropped(self):
        rec = user("p01", "x")
        del rec["promptId"]
        self.assertIsNone(turns.user_prompt_text(rec))
        self.assertIsNone(turns.user_prompt_text(user("p01", "x", attachment={"a": 1})))

    def test_tool_result_envelopes_are_dropped(self):
        rec = user("p01", "x")
        rec["message"]["content"] = [{"type": "tool_result", "content": "ok"}]
        self.assertIsNone(turns.user_prompt_text(rec))

    def test_sidechain_and_headless_records_are_dropped(self):
        self.assertIsNone(turns.user_prompt_text(user("p01", "x", isSidechain=True)))
        self.assertIsNone(turns.user_prompt_text(user("p01", "x", entrypoint="sdk-cli")))
        self.assertIsNone(turns.assistant_text(asst("x", isSidechain=True)))
        self.assertIsNone(turns.assistant_text(asst("x", entrypoint="sdk-cli")))

    def test_task_notifications_are_not_the_user(self):
        self.assertIsNone(
            turns.user_prompt_text(
                user("p01", "x", origin={"kind": "task-notification"}, promptSource="system")
            )
        )
        self.assertIsNone(turns.user_prompt_text(user("p01", "x", promptSource="system")))

    def test_legacy_records_without_origin_are_kept(self):
        rec = user("p01", "x")
        del rec["origin"]
        del rec["promptSource"]
        self.assertEqual(turns.user_prompt_text(rec), "x")

    def test_slash_command_invocations_are_dropped(self):
        self.assertIsNone(
            turns.user_prompt_text(
                user("p01", "<command-name>/wrapup</command-name>\n<command-message>wrapup")
            )
        )
        self.assertIsNone(
            turns.user_prompt_text(user("p01", "<local-command-stdout></local-command-stdout>"))
        )
        self.assertIsNone(turns.user_prompt_text(user("p01", "   ")))

    def test_assistant_text_joins_text_blocks_and_drops_tool_use(self):
        self.assertEqual(turns.assistant_text(asst("one", "two")), "one\n\ntwo")
        self.assertIsNone(turns.assistant_text(asst()))
        self.assertIsNone(turns.assistant_text(user("p01", "x")))


class ExtractPairsTests(unittest.TestCase):
    def test_pairs_previous_assistant_with_each_prompt(self):
        state = turns.PairState()
        pairs = turns.extract_pairs(
            [asst("A0"), user("p01", "U1"), asst("A1a", "A1b"), user("p02", "U2"), asst("A2")],
            state,
        )
        self.assertEqual(
            [(p["assistant"], p["user"]) for p in pairs], [("A0", "U1"), ("A1a\n\nA1b", "U2")]
        )
        self.assertEqual(pairs[0]["prompt_id"], "p01")
        self.assertEqual(state.carry, "A2")

    def test_first_prompt_without_prior_assistant_yields_no_pair(self):
        state = turns.PairState()
        pairs = turns.extract_pairs([user("p01", "U1"), asst("A1")], state)
        self.assertEqual(pairs, [])
        self.assertEqual(state.carry, "A1")

    def test_carry_bridges_two_calls(self):
        state = turns.PairState()
        turns.extract_pairs([asst("A0"), user("p01", "U1"), asst("A1")], state)
        pairs = turns.extract_pairs([user("p02", "U2"), asst("A2")], state)
        self.assertEqual([(p["assistant"], p["user"]) for p in pairs], [("A1", "U2")])
        self.assertEqual(state.carry, "A2")

    def test_interrupted_turn_keeps_both_halves(self):
        state = turns.PairState()
        turns.extract_pairs([user("p01", "U1"), asst("A1 first half")], state)
        pairs = turns.extract_pairs([asst("A1 second half"), user("p02", "U2")], state)
        self.assertEqual(pairs[0]["assistant"], "A1 first half\n\nA1 second half")
        self.assertIsNone(state.carry)

    def test_seen_prompt_ids_are_not_re_emitted(self):
        state = turns.PairState(seen_prompt_ids={"p01"})
        pairs = turns.extract_pairs([asst("A0"), user("p01", "U1")], state)
        self.assertEqual(pairs, [])

    def test_long_texts_are_bounded(self):
        state = turns.PairState()
        long_a = "a" * (turns.ASSISTANT_MAX_CHARS + 100) + "TAIL"
        long_u = "HEAD" + "u" * (turns.USER_MAX_CHARS + 100)
        pairs = turns.extract_pairs([asst(long_a), user("p01", long_u)], state)
        self.assertEqual(len(pairs[0]["assistant"]), turns.ASSISTANT_MAX_CHARS)
        self.assertTrue(pairs[0]["assistant"].startswith("..."))
        self.assertTrue(pairs[0]["assistant"].endswith("TAIL"))
        self.assertEqual(len(pairs[0]["user"]), turns.USER_MAX_CHARS)
        self.assertTrue(pairs[0]["user"].startswith("HEAD"))


class IncrementalUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.state = root / "state"
        self.transcript = root / "session.jsonl"
        patcher = mock.patch.dict(os.environ, {store.TARGET_STATE_DIR_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, records: list[dict], *, mode: str = "w", newline: bool = True) -> None:
        with self.transcript.open(mode, encoding="utf-8") as f:
            for i, rec in enumerate(records):
                f.write(json.dumps(rec))
                if newline or i < len(records) - 1:
                    f.write("\n")

    def test_incremental_calls_only_add_new_pairs(self):
        self._write([asst("A0"), user("p01", "U1"), asst("A1")])
        self.assertEqual(len(turns.update_from_transcript(self.transcript, SID)), 1)
        self._write([user("p02", "U2"), asst("A2")], mode="a")
        new = turns.update_from_transcript(self.transcript, SID)
        self.assertEqual([(p["assistant"], p["user"]) for p in new], [("A1", "U2")])
        self.assertEqual(turns.update_from_transcript(self.transcript, SID), [])
        self.assertEqual(len(turns.load_pairs(SID)), 2)
        self.assertEqual(turns.list_turn_sessions(), ["abcd1234"])
        cursor = json.loads(store.cursor_path(SID).read_text(encoding="utf-8"))
        self.assertEqual(cursor["offset"], self.transcript.stat().st_size)
        self.assertEqual(cursor["carry"], "A2")

    def test_partial_trailing_line_waits_for_the_next_call(self):
        self._write([asst("A0"), user("p01", "U1"), asst("A1")])
        turns.update_from_transcript(self.transcript, SID)
        self._write([user("p02", "U2")], mode="a", newline=False)
        self.assertEqual(turns.update_from_transcript(self.transcript, SID), [])
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual(len(turns.update_from_transcript(self.transcript, SID)), 1)

    def test_shrunk_transcript_resets_without_duplicating(self):
        records = [asst("A0"), user("p01", "U1"), asst("A1"), user("p02", "U2"), asst("A2")]
        self._write(records)
        self.assertEqual(len(turns.update_from_transcript(self.transcript, SID)), 2)
        self._write(records[:3])  # shorter than the cursor offset -> reset
        self.assertEqual(turns.update_from_transcript(self.transcript, SID), [])
        self.assertEqual(len(turns.load_pairs(SID)), 2)

    def test_missing_transcript_is_a_no_op(self):
        self.assertEqual(turns.update_from_transcript(self.transcript, SID), [])
        self.assertFalse(store.turns_path(SID).exists())


if __name__ == "__main__":
    unittest.main()
