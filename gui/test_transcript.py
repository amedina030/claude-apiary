"""Tests for the message filter and JSONL parsing."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from gui.transcript import (
    Message,
    SessionDiscovery,
    TokenUsage,
    TranscriptTail,
    filter_record,
    find_active_session_jsonl,
    parse_jsonl_lines,
    snapshot_existing_jsonls,
)


class FilterRecordTests(unittest.TestCase):
    def test_real_user_prompt_kept(self):
        rec = {
            "type": "user",
            "promptId": "abc-123",
            "message": {"role": "user", "content": "hello world"},
            "uuid": "u1",
            "timestamp": "2026-04-18T19:47:00.025Z",
        }
        msg = filter_record(rec)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.text, "hello world")

    def test_user_attachment_dropped(self):
        rec = {
            "type": "user",
            "attachment": {"type": "deferred_tools_delta", "addedNames": ["X"]},
            "uuid": "u2",
        }
        self.assertIsNone(filter_record(rec))

    def test_user_tool_result_dropped(self):
        rec = {
            "type": "user",
            "promptId": "p1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        }
        self.assertIsNone(filter_record(rec))

    def test_user_no_promptid_dropped(self):
        rec = {
            "type": "user",
            "message": {"role": "user", "content": "synthetic"},
        }
        self.assertIsNone(filter_record(rec))

    def test_assistant_text_extracted_and_tool_use_dropped(self):
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll read the file."},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            },
            "uuid": "a1",
            "timestamp": "2026-04-18T19:48:00Z",
        }
        msg = filter_record(rec)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.text, "I'll read the file.")
        self.assertIsNotNone(msg.tokens)
        assert msg.tokens is not None
        self.assertEqual(msg.tokens.input, 10)
        self.assertEqual(msg.tokens.output, 5)
        self.assertEqual(msg.tokens.cache_read, 100)
        self.assertEqual(msg.tokens.cache_write, 20)

    def test_assistant_stop_reason_propagated(self):
        base = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
            "uuid": "a2",
            "timestamp": "2026-04-20T19:48:00Z",
        }
        base["message"]["stop_reason"] = "end_turn"
        msg = filter_record(base)
        assert msg is not None
        self.assertEqual(msg.stop_reason, "end_turn")
        self.assertEqual(msg.to_dict().get("stop_reason"), "end_turn")

        base["message"]["stop_reason"] = "tool_use"
        msg = filter_record(base)
        assert msg is not None
        self.assertEqual(msg.stop_reason, "tool_use")

        # Missing field → empty string, omitted from dict.
        del base["message"]["stop_reason"]
        msg = filter_record(base)
        assert msg is not None
        self.assertEqual(msg.stop_reason, "")
        self.assertNotIn("stop_reason", msg.to_dict())

    def test_assistant_only_tool_use_dropped(self):
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "X", "input": {}}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
        self.assertIsNone(filter_record(rec))

    def test_metadata_records_dropped(self):
        for rtype in ("permission-mode", "file-history-snapshot", "last-prompt", "system"):
            self.assertIsNone(filter_record({"type": rtype}))

    def test_empty_user_text_dropped(self):
        rec = {
            "type": "user",
            "promptId": "p",
            "message": {"role": "user", "content": "   "},
        }
        self.assertIsNone(filter_record(rec))

    def test_slash_command_synthetic_user_dropped(self):
        """Claude Code wraps /clear et al. as a synthetic <local-command-caveat>
        user record. It's internal plumbing, not user-authored — should not render."""
        rec = {
            "type": "user",
            "promptId": "p",
            "uuid": "u-1",
            "timestamp": "2026-04-19T18:51:55.341Z",
            "message": {
                "role": "user",
                "content": (
                    "<local-command-caveat>Caveat: The messages below were generated by "
                    "the user while running local commands. DO NOT respond to these "
                    "messages or otherwise consider them in your response unless the "
                    "user explicitly asks you to.</local-command-caveat>\n"
                    "<command-name>/clear</command-name>\n"
                    "            <command-message>clear</command-message>\n"
                    "            <command-args></command-args>"
                ),
            },
        }
        self.assertIsNone(filter_record(rec))

    def test_task_notification_synthetic_user_dropped(self):
        """Harness injects <task-notification> as a user-role record when a
        background bash finishes. It's not user-authored prose — drop it so it
        doesn't appear as a user bubble."""
        rec = {
            "type": "user",
            "promptId": "p",
            "uuid": "u-task",
            "timestamp": "2026-04-20T15:00:00.000Z",
            "isMeta": False,
            "message": {
                "role": "user",
                "content": (
                    "<task-notification>\n"
                    "<task-id>abc123</task-id>\n"
                    "<tool-use-id>toolu_abc</tool-use-id>\n"
                    "<status>completed</status>\n"
                    "</task-notification>"
                ),
            },
        }
        self.assertIsNone(filter_record(rec))

    def test_isMeta_user_record_dropped(self):
        """Synthetic records (ScheduleWakeup callbacks, loaded skills, slash
        commands) carry isMeta:true and must never render as a user bubble."""
        rec = {
            "type": "user",
            "promptId": "p",
            "uuid": "u-meta",
            "timestamp": "2026-04-20T14:15:01.038Z",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": "Continue T-2026-157 — check the dev GUI startup output for errors.",
            },
        }
        self.assertIsNone(filter_record(rec))

    def test_isMeta_false_user_record_kept(self):
        """isMeta is only suppressive when truthy; false/missing must not block real messages."""
        rec = {
            "type": "user",
            "promptId": "p",
            "uuid": "u-not-meta",
            "timestamp": "2026-04-20T14:15:01.038Z",
            "isMeta": False,
            "message": {"role": "user", "content": "real user message"},
        }
        self.assertIsNotNone(filter_record(rec))

    def test_user_content_that_mentions_tag_inline_still_rendered(self):
        """Guard against over-filtering: tag name appearing mid-content stays."""
        rec = {
            "type": "user",
            "promptId": "p",
            "uuid": "u-2",
            "timestamp": "2026-04-19T18:51:55.341Z",
            "message": {
                "role": "user",
                "content": "how do I handle <local-command-caveat> blocks in output?",
            },
        }
        self.assertIsNotNone(filter_record(rec))

    def test_malformed_inputs(self):
        self.assertIsNone(filter_record({}))
        self.assertIsNone(filter_record({"type": "user"}))
        self.assertIsNone(filter_record({"type": "assistant", "message": {}}))


class ParseJsonlLinesTests(unittest.TestCase):
    def test_skips_blank_and_malformed(self):
        text = "\n".join(
            [
                "",
                "not json",
                json.dumps(
                    {
                        "type": "user",
                        "promptId": "p",
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
                "{ partial",
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hey"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    }
                ),
            ]
        )
        msgs = parse_jsonl_lines(text)
        self.assertEqual([m.role for m in msgs], ["user", "assistant"])
        self.assertEqual([m.text for m in msgs], ["hi", "hey"])


class TokenUsageTests(unittest.TestCase):
    def test_add(self):
        a = TokenUsage(input=1, output=2, cache_read=3, cache_write=4)
        b = TokenUsage(input=10, output=20, cache_read=30, cache_write=40)
        a.add(b)
        self.assertEqual(
            a.to_dict(), {"input": 11, "output": 22, "cache_read": 33, "cache_write": 44}
        )


class TranscriptTailTests(unittest.TestCase):
    def test_replays_existing_then_tails_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session.jsonl"
            existing = [
                json.dumps(
                    {
                        "type": "user",
                        "promptId": "p1",
                        "message": {"role": "user", "content": "first"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "reply one"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    }
                ),
            ]
            p.write_text("\n".join(existing) + "\n", encoding="utf-8")

            received: list[Message] = []
            tail = TranscriptTail(p, on_message=received.append, poll_interval=0.05)
            tail.start()
            try:
                # Wait for replay.
                deadline = time.time() + 2.0
                while time.time() < deadline and len(received) < 2:
                    time.sleep(0.05)
                self.assertEqual([m.text for m in received], ["first", "reply one"])

                # Append a new line and confirm it is picked up.
                with p.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "type": "user",
                                "promptId": "p2",
                                "message": {"role": "user", "content": "second"},
                            }
                        )
                        + "\n"
                    )
                deadline = time.time() + 2.0
                while time.time() < deadline and len(received) < 3:
                    time.sleep(0.05)
                self.assertEqual([m.text for m in received], ["first", "reply one", "second"])
            finally:
                tail.stop()

    def test_start_at_skips_replayed_bytes_and_catches_the_gap(self):
        """The attach race: the caller replays what it read and hands the
        tail that byte count. A record written between the read and the
        tail start must be rendered exactly once; the replayed one never."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session.jsonl"
            first = json.dumps(
                {
                    "type": "user",
                    "promptId": "p1",
                    "message": {"role": "user", "content": "already replayed"},
                }
            )
            p.write_text(first + "\n", encoding="utf-8")
            raw = p.read_bytes()
            # Claude appends before the tail thread starts.
            gap = json.dumps(
                {
                    "type": "user",
                    "promptId": "p2",
                    "message": {"role": "user", "content": "in the gap"},
                }
            )
            with p.open("a", encoding="utf-8") as f:
                f.write(gap + "\n")
            received: list[Message] = []
            tail = TranscriptTail(
                p, on_message=received.append, poll_interval=0.05, start_at=len(raw)
            )
            tail.start()
            try:
                deadline = time.time() + 2.0
                while time.time() < deadline and not received:
                    time.sleep(0.05)
                time.sleep(0.15)
                self.assertEqual([m.text for m in received], ["in the gap"])
            finally:
                tail.stop()

    def test_multibyte_utf8_survives_byte_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session.jsonl"
            p.write_text("", encoding="utf-8")
            received: list[Message] = []
            tail = TranscriptTail(p, on_message=received.append, poll_interval=0.05)
            tail.start()
            try:
                rec = json.dumps(
                    {
                        "type": "user",
                        "promptId": "p1",
                        "message": {"role": "user", "content": "héllo — ✓ 日本"},
                    },
                    ensure_ascii=False,
                )
                with p.open("a", encoding="utf-8") as f:
                    f.write(rec + "\n")
                deadline = time.time() + 2.0
                while time.time() < deadline and not received:
                    time.sleep(0.05)
                self.assertEqual([m.text for m in received], ["héllo — ✓ 日本"])
            finally:
                tail.stop()

    def test_truncated_file_is_reparsed_from_the_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session.jsonl"
            long_rec = json.dumps(
                {
                    "type": "user",
                    "promptId": "p1",
                    "message": {"role": "user", "content": "x" * 500},
                }
            )
            p.write_text(long_rec + "\n", encoding="utf-8")
            received: list[Message] = []
            tail = TranscriptTail(p, on_message=received.append, poll_interval=0.05)
            tail.start()
            try:
                deadline = time.time() + 2.0
                while time.time() < deadline and not received:
                    time.sleep(0.05)
                self.assertEqual(len(received), 1)
                # Rewritten shorter (/clear, compaction): the old offset is past EOF.
                short_rec = json.dumps(
                    {
                        "type": "user",
                        "promptId": "p2",
                        "message": {"role": "user", "content": "fresh"},
                    }
                )
                p.write_text(short_rec + "\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and len(received) < 2:
                    time.sleep(0.05)
                self.assertEqual([m.text for m in received][-1], "fresh")
            finally:
                tail.stop()

    def test_partial_line_buffered_until_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session.jsonl"
            p.write_text("", encoding="utf-8")
            received: list[Message] = []
            tail = TranscriptTail(p, on_message=received.append, poll_interval=0.05)
            tail.start()
            try:
                # Write a partial JSONL line (no trailing newline).
                rec_str = json.dumps(
                    {
                        "type": "user",
                        "promptId": "p1",
                        "message": {"role": "user", "content": "partial"},
                    }
                )
                half = len(rec_str) // 2
                with p.open("a", encoding="utf-8") as f:
                    f.write(rec_str[:half])
                    f.flush()
                time.sleep(0.2)
                self.assertEqual(received, [])
                with p.open("a", encoding="utf-8") as f:
                    f.write(rec_str[half:] + "\n")
                deadline = time.time() + 2.0
                while time.time() < deadline and not received:
                    time.sleep(0.05)
                self.assertEqual([m.text for m in received], ["partial"])
            finally:
                tail.stop()


class FindActiveSessionTests(unittest.TestCase):
    def test_picks_most_recently_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            old = root / "a" / "old.jsonl"
            new = root / "b" / "new.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            time.sleep(0.05)
            new.write_text("{}\n", encoding="utf-8")
            self.assertEqual(find_active_session_jsonl(root), new)

    def test_returns_none_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_active_session_jsonl(Path(tmp)))

    def test_excludes_pre_existing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            parent = root / "a" / "parent.jsonl"
            parent.write_text("{}\n", encoding="utf-8")
            time.sleep(0.05)
            spawned = root / "a" / "spawned.jsonl"
            spawned.write_text("{}\n", encoding="utf-8")
            # Even though parent gets modified again later, exclude_paths keeps it out.
            time.sleep(0.05)
            parent.write_text("{}\n", encoding="utf-8")
            picked = find_active_session_jsonl(root, exclude_paths={parent})
            self.assertEqual(picked, spawned)

    def test_min_ctime_filters_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            old = root / "a" / "old.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            time.sleep(0.1)
            cutoff = time.time()
            time.sleep(0.1)
            fresh = root / "a" / "fresh.jsonl"
            fresh.write_text("{}\n", encoding="utf-8")
            picked = find_active_session_jsonl(root, min_ctime=cutoff)
            self.assertEqual(picked, fresh)
            # `old` would have been picked without the pin (it's the only file pre-cutoff).
            self.assertEqual(find_active_session_jsonl(root, min_ctime=0), fresh)


class SnapshotExistingJsonlsTests(unittest.TestCase):
    def test_returns_set_of_present_jsonls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p1").mkdir()
            (root / "p2").mkdir()
            a = root / "p1" / "a.jsonl"
            b = root / "p2" / "b.jsonl"
            a.write_text("{}\n", encoding="utf-8")
            b.write_text("{}\n", encoding="utf-8")
            (root / "p1" / "not-jsonl.txt").write_text("hi", encoding="utf-8")
            self.assertEqual(snapshot_existing_jsonls(root), {a, b})


class SessionDiscoveryTests(unittest.TestCase):
    def test_fires_on_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            switches: list[Path] = []
            disc = SessionDiscovery(
                on_switch=switches.append, projects_root=root, poll_interval=0.05
            )
            disc.start()
            try:
                first = root / "first.jsonl"
                first.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and not switches:
                    time.sleep(0.05)
                self.assertEqual(switches, [first])

                time.sleep(0.1)
                second = root / "second.jsonl"
                second.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and len(switches) < 2:
                    time.sleep(0.05)
                self.assertEqual(switches, [first, second])
            finally:
                disc.stop()

    def test_pin_excludes_pre_spawn_jsonls_even_when_modified(self):
        """The whole point of the pin: writes to the parent terminal's JSONL must not flap us back."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent.jsonl"
            parent.write_text("{}\n", encoding="utf-8")
            pre_existing = {parent}
            spawn_time = time.time() + 0.05  # set pin slightly in the future

            switches: list[Path] = []
            disc = SessionDiscovery(
                on_switch=switches.append,
                projects_root=root,
                poll_interval=0.05,
                min_ctime=spawn_time,
                exclude_paths=pre_existing,
            )
            disc.start()
            try:
                # Modify parent — should NOT trigger a switch (excluded).
                time.sleep(0.2)
                parent.write_text("{}\n{}\n", encoding="utf-8")
                time.sleep(0.3)
                self.assertEqual(switches, [])

                # Now create a brand-new JSONL post-spawn — should fire.
                spawned = root / "spawned.jsonl"
                spawned.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and not switches:
                    time.sleep(0.05)
                self.assertEqual(switches, [spawned])

                # Touch parent again — still must not flap.
                time.sleep(0.1)
                parent.write_text("{}\n{}\n{}\n", encoding="utf-8")
                time.sleep(0.3)
                self.assertEqual(switches, [spawned])
            finally:
                disc.stop()

    def test_lock_after_first_ignores_later_jsonls(self):
        """Once locked onto the first matching JSONL, later post-spawn JSONLs (subagent
        sidechains, etc.) must not steal focus."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            switches: list[Path] = []
            disc = SessionDiscovery(
                on_switch=switches.append,
                projects_root=root,
                poll_interval=0.05,
                lock_after_first=True,
            )
            disc.start()
            try:
                first = root / "main.jsonl"
                first.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and not switches:
                    time.sleep(0.05)
                self.assertEqual(switches, [first])

                # Subagent JSONL appears later — must NOT trigger a switch.
                time.sleep(0.1)
                sidechain = root / "subagent.jsonl"
                sidechain.write_text("{}\n", encoding="utf-8")
                time.sleep(0.4)
                self.assertEqual(switches, [first])

                # Even if subagent gets touched/grown, no switch.
                sidechain.write_text("{}\n{}\n", encoding="utf-8")
                time.sleep(0.3)
                self.assertEqual(switches, [first])
            finally:
                disc.stop()

    def test_set_pin_after_start_takes_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent.jsonl"
            parent.write_text("{}\n", encoding="utf-8")

            switches: list[Path] = []
            disc = SessionDiscovery(
                on_switch=switches.append, projects_root=root, poll_interval=0.05
            )
            disc.start()
            try:
                # Without pin, parent gets picked up.
                deadline = time.time() + 1.0
                while time.time() < deadline and not switches:
                    time.sleep(0.05)
                self.assertEqual(switches, [parent])

                # Apply pin → discovery should drop parent and wait for a fresh JSONL.
                disc.set_pin(min_ctime=time.time() + 0.05, exclude_paths={parent})
                time.sleep(0.2)
                # Touching parent shouldn't add a new switch.
                parent.write_text("{}\n{}\n", encoding="utf-8")
                time.sleep(0.3)
                self.assertEqual(switches, [parent])  # no new switches

                spawned = root / "spawned.jsonl"
                spawned.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and len(switches) < 2:
                    time.sleep(0.05)
                self.assertEqual(switches, [parent, spawned])
            finally:
                disc.stop()

    def test_re_pin_after_lock_unlatches_for_new_jsonl(self):
        """Models the /clear flow: discovery locked onto JSONL A via lock_after_first,
        then set_pin bumps min_ctime so a fresh post-clear JSONL B takes over."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "pre-clear.jsonl"
            first.write_text("{}\n", encoding="utf-8")

            switches: list[Path] = []
            disc = SessionDiscovery(
                on_switch=switches.append,
                projects_root=root,
                poll_interval=0.05,
                lock_after_first=True,
            )
            disc.start()
            try:
                deadline = time.time() + 2.0
                while time.time() < deadline and not switches:
                    time.sleep(0.05)
                self.assertEqual(switches, [first])

                # Simulate /clear: re-pin with min_ctime past first.jsonl's ctime.
                # set_pin drops self._current because its ctime is now stale.
                disc.set_pin(min_ctime=time.time() + 0.05, lock_after_first=True)

                # Touching the old JSONL must not re-latch (it's older than min_ctime).
                time.sleep(0.15)
                first.write_text("{}\n{}\n", encoding="utf-8")
                time.sleep(0.2)
                self.assertEqual(switches, [first])

                # New JSONL appears after /clear — should be picked up.
                fresh = root / "post-clear.jsonl"
                fresh.write_text("{}\n", encoding="utf-8")
                deadline = time.time() + 2.0
                while time.time() < deadline and len(switches) < 2:
                    time.sleep(0.05)
                self.assertEqual(switches, [first, fresh])
            finally:
                disc.stop()


if __name__ == "__main__":
    unittest.main()
