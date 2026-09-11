#!/usr/bin/env python3
"""Tests for telephone/store.py: ids, layout, records and exchanges."""

import json
import tempfile
import unittest
from pathlib import Path

from telephone import store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.apiary = Path(self._tmp.name) / "apiary"
        self.apiary.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)


class TestPathsAndIds(StoreTestCase):
    def test_store_dir_is_under_dot_apiary_not_dot_repos(self):
        # `apiary doctor orphans` walks .repos/ for folders with no registry
        # entry; telephone records must never land there.
        path = store.store_dir(self.apiary)
        self.assertEqual(path, self.apiary / ".apiary" / "telephone")
        self.assertNotIn(".repos", path.parts)

    def test_first_call_creates_directory_year_and_counter(self):
        self.assertFalse(store.store_dir(self.apiary).exists())
        call_id = store.allocate_id(self.apiary)
        year = call_id.split("-")[1]
        folder = store.store_dir(self.apiary) / year
        self.assertTrue(folder.is_dir())
        self.assertTrue((folder / store.NEXT_SEQ_FILENAME).is_file())
        self.assertEqual(call_id, f"C-{year}-1")

    def test_ids_are_sequential_and_distinct(self):
        ids = [store.allocate_id(self.apiary) for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)
        seqs = [store.parse_id(i)[1] for i in ids]
        self.assertEqual(seqs, [1, 2, 3])

    def test_parse_id_rejects_other_shapes(self):
        for bad in ("", "C-2026", "T-2026-1", "2026-1", "C-26-1", None):
            with self.assertRaises((ValueError, TypeError)):
                store.parse_id(bad)

    def test_deleted_counter_is_rebuilt_from_the_files_on_disk(self):
        first = store.allocate_id(self.apiary)
        path = store.record_path(first, self.apiary)
        store.write_record(path, {"id": first}, "body\n")
        (path.parent / store.NEXT_SEQ_FILENAME).unlink()
        second = store.allocate_id(self.apiary)
        self.assertEqual(store.parse_id(second)[1], 2)


class TestRecordIO(StoreTestCase):
    def _make(self, **extra):
        call_id = store.allocate_id(self.apiary)
        path = store.record_path(call_id, self.apiary)
        meta = {
            "id": call_id,
            "mode": store.MODE_ANSWER,
            "status": store.STATUS_OPEN,
            "caller": "caller",
            "callee": "callee",
            "exchanges": "0",
            **extra,
        }
        store.write_record(path, meta, f"# Call {call_id}\n")
        return call_id, path

    def test_round_trip_through_the_shared_frontmatter_dialect(self):
        call_id, path = self._make(issue="")
        meta, body = store.read_record(path)
        self.assertEqual(meta["id"], call_id)
        self.assertEqual(meta["status"], store.STATUS_OPEN)
        self.assertIn(f"# Call {call_id}", body)

    def test_load_raises_for_a_missing_record(self):
        with self.assertRaises(FileNotFoundError):
            store.load("C-2026-999", self.apiary)

    def test_list_records_is_newest_first_and_filters(self):
        a, _ = self._make()
        b, path_b = self._make(status=store.STATUS_ANSWERED, callee="other")
        rows = store.list_records(self.apiary)
        self.assertEqual([r["id"] for r in rows], [b, a])
        self.assertEqual([r["id"] for r in store.list_records(self.apiary, callee="other")], [b])
        answered = store.list_records(self.apiary, status=store.STATUS_ANSWERED)
        self.assertEqual([r["id"] for r in answered], [b])
        self.assertTrue(Path(answered[0]["path"]) == path_b)

    def test_list_records_on_an_empty_store_is_empty(self):
        self.assertEqual(store.list_records(self.apiary), [])

    def test_open_act_call_finds_only_open_act_calls(self):
        self.assertIsNone(store.open_act_call("callee", self.apiary))
        self._make(mode=store.MODE_ACT, status=store.STATUS_ANSWERED)
        self.assertIsNone(store.open_act_call("callee", self.apiary))
        act_id, _ = self._make(mode=store.MODE_ACT, status=store.STATUS_OPEN)
        self.assertEqual(store.open_act_call("callee", self.apiary), act_id)


class TestExchanges(StoreTestCase):
    def test_append_counts_exchanges_and_keeps_earlier_ones(self):
        call_id = store.allocate_id(self.apiary)
        path = store.record_path(call_id, self.apiary)
        store.write_record(path, {"id": call_id, "exchanges": "0"}, "# head\n")
        store.append_exchange(
            path,
            {"status": store.STATUS_ANSWERED},
            store.render_exchange(1, sent="q1", reply="a1", status="answered", at="t1"),
        )
        meta = store.append_exchange(
            path,
            {},
            store.render_exchange(2, sent="q2", reply="a2", status="answered", at="t2"),
        )
        self.assertEqual(meta["exchanges"], "2")
        _meta, body = store.read_record(path)
        self.assertEqual(store.exchange_count(body), 2)
        items = store.parse_exchanges(body)
        self.assertEqual([i["number"] for i in items], ["1", "2"])
        self.assertEqual(items[0]["message"], "q1")
        self.assertEqual(items[1]["reply"], "a2")

    def test_multiline_bodies_survive_the_round_trip(self):
        call_id = store.allocate_id(self.apiary)
        path = store.record_path(call_id, self.apiary)
        store.write_record(path, {"id": call_id}, "# head\n")
        reply = "line one\n\nline two with a #hash and a - dash"
        store.append_exchange(
            path, {}, store.render_exchange(1, sent="ask", reply=reply, status="answered", at="t")
        )
        _meta, body = store.read_record(path)
        self.assertEqual(store.parse_exchanges(body)[0]["reply"], reply)

    def test_notes_are_recorded_on_the_exchange(self):
        section = store.render_exchange(
            1, sent="q", reply="a", status="answered", at="t", notes=["issue: push detected"]
        )
        self.assertIn("issue: push detected", section)


class TestConfig(StoreTestCase):
    def test_shipped_config_has_the_documented_caps(self):
        cfg = store.load_config()
        self.assertEqual(cfg["max_autonomous_calls_per_session"], 3)
        self.assertEqual(cfg["max_autonomous_exchanges_per_line"], 6)
        self.assertEqual(store.mode_config(cfg, "answer")["timeout_seconds"], 600)
        self.assertEqual(store.mode_config(cfg, "act")["timeout_seconds"], 1800)

    def test_act_mode_accepts_edits_and_answer_mode_denies_them(self):
        cfg = store.load_config()
        answer = store.mode_config(cfg, "answer")
        act = store.mode_config(cfg, "act")
        self.assertIn("Write", answer["disallowed_tools"])
        self.assertIn("Edit", answer["disallowed_tools"])
        self.assertNotIn("Write", answer["allowed_tools"])
        self.assertEqual(act["permission_mode"], "acceptEdits")
        self.assertIn("Write", act["allowed_tools"])
        for mode in (answer, act):
            self.assertTrue(any("git push" in rule for rule in mode["disallowed_tools"]))

    def test_a_malformed_config_falls_back_to_the_defaults(self):
        bad = Path(self._tmp.name) / "config.json"
        bad.write_text("{not json", encoding="utf-8")
        cfg = store.load_config(bad)
        self.assertEqual(cfg["max_autonomous_calls_per_session"], 3)

    def test_a_partial_config_keeps_the_shipped_tool_lists(self):
        partial = Path(self._tmp.name) / "partial.json"
        partial.write_text(json.dumps({"answer": {"timeout_seconds": 5}}), encoding="utf-8")
        answer = store.mode_config(store.load_config(partial), "answer")
        self.assertEqual(answer["timeout_seconds"], 5)
        self.assertIn("Read", answer["allowed_tools"])

    def test_unknown_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            store.mode_config(store.load_config(), "shout")

    def test_the_shipped_config_has_a_grant_ttl(self):
        self.assertEqual(store.load_config()["grant_ttl_seconds"], 900)

    def test_answer_mode_has_no_blanket_python(self):
        answer = store.mode_config(store.load_config(), "answer")
        self.assertNotIn("Bash(python *)", answer["allowed_tools"])
        self.assertIn("Bash(python * scribe/notes.py *)", answer["allowed_tools"])


class TestExchangeParsing(StoreTestCase):
    def test_a_reply_that_contains_an_exchange_heading_does_not_split_the_record(self):
        tricky = "## Exchange 2\nis a heading the callee happened to write\n\n## Answer\nfine"
        body = "# head\n\n" + store.render_exchange(
            1, sent="q1", reply=tricky, status="answered", at="2026-09-11T00:00:00Z"
        )
        self.assertEqual(store.exchange_count(body), 1)
        items = store.parse_exchanges(body)
        self.assertEqual(len(items), 1)
        self.assertIn("is a heading the callee happened to write", items[0]["reply"])

    def test_a_message_that_contains_an_exchange_heading_does_not_split_either(self):
        body = "# head\n\n" + store.render_exchange(
            1, sent="## Exchange 9\n\n- at: fake", reply="a1", status="answered", at="t"
        )
        body += "\n" + store.render_exchange(2, sent="q2", reply="a2", status="answered", at="t")
        self.assertEqual(store.exchange_count(body), 2)
        self.assertEqual([i["number"] for i in store.parse_exchanges(body)], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
