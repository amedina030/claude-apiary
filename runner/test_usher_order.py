#!/usr/bin/env python3
"""Tests for runner/usher_order.py — order manifest management."""
import json
import tempfile
import unittest
from pathlib import Path

from runner.usher_order import (
    load_order,
    save_order,
    register_standalone,
    register_group,
    next_eligible,
    update_status,
    cascade_failure,
    archive_order,
    detect_stale_running,
)


class TestLoadSaveOrder(unittest.TestCase):

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            order = load_order(path)
            self.assertEqual(order, {"groups": [], "standalone": []})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            order = {
                "groups": [{"group_id": "g1", "origin": "test", "tickets": []}],
                "standalone": [{"uuid": "u1", "slug": "s1", "status": "pending"}],
            }
            save_order(order, path)
            loaded = load_order(path)
            self.assertEqual(loaded, order)

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            path.write_text("not json", encoding="utf-8")
            order = load_order(path)
            self.assertEqual(order, {"groups": [], "standalone": []})

    def test_non_dict_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            path.write_text("[]", encoding="utf-8")
            order = load_order(path)
            self.assertEqual(order, {"groups": [], "standalone": []})

    def test_atomic_write_no_temp_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_order({"groups": [], "standalone": []}, path)
            tmp_file = path.with_suffix(".tmp")
            self.assertFalse(tmp_file.exists())


class TestRegister(unittest.TestCase):

    def test_register_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("uuid-1", "my-ticket", path)
            order = load_order(path)
            self.assertEqual(len(order["standalone"]), 1)
            self.assertEqual(order["standalone"][0]["uuid"], "uuid-1")
            self.assertEqual(order["standalone"][0]["status"], "pending")

    def test_register_standalone_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("uuid-1", "my-ticket", path)
            register_standalone("uuid-1", "my-ticket", path)
            order = load_order(path)
            self.assertEqual(len(order["standalone"]), 1)

    def test_register_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            tickets = [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
            ]
            register_group("g1", "test origin", tickets, path)
            order = load_order(path)
            self.assertEqual(len(order["groups"]), 1)
            g = order["groups"][0]
            self.assertEqual(g["group_id"], "g1")
            self.assertEqual(len(g["tickets"]), 2)
            self.assertEqual(g["tickets"][0]["status"], "pending")
            self.assertEqual(g["tickets"][1]["depends_on"], ["a"])


class TestNextEligible(unittest.TestCase):

    def _make_order(self, tmp):
        return Path(tmp) / "order.json"

    def test_standalone_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_standalone("s1", "standalone-ticket", path)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            result = next_eligible(path)
            self.assertIsNotNone(result)
            self.assertEqual(result["uuid"], "s1")

    def test_group_no_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            result = next_eligible(path)
            self.assertIsNotNone(result)
            self.assertEqual(result["uuid"], "a")
            self.assertEqual(result["group_id"], "g1")

    def test_group_with_deps_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
            ], path)
            update_status("a", "completed", path)
            result = next_eligible(path)
            self.assertEqual(result["uuid"], "b")

    def test_group_with_deps_unmet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
            ], path)
            # a is still pending, so b is not eligible
            result = next_eligible(path)
            self.assertEqual(result["uuid"], "a")

    def test_nothing_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            update_status("a", "completed", path)
            result = next_eligible(path)
            self.assertIsNone(result)

    def test_blocked_tickets_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_order(tmp)
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
            ], path)
            update_status("a", "failed", path)
            cascade_failure("a", path)
            result = next_eligible(path)
            self.assertIsNone(result)


class TestUpdateStatus(unittest.TestCase):

    def test_update_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("u1", "ticket", path)
            self.assertTrue(update_status("u1", "running", path))
            order = load_order(path)
            self.assertEqual(order["standalone"][0]["status"], "running")

    def test_update_in_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            self.assertTrue(update_status("a", "completed", path))
            order = load_order(path)
            self.assertEqual(order["groups"][0]["tickets"][0]["status"], "completed")

    def test_update_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_order({"groups": [], "standalone": []}, path)
            self.assertFalse(update_status("nonexistent", "running", path))

    def test_invalid_status_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("u1", "ticket", path)
            with self.assertRaises(ValueError):
                update_status("u1", "invalid_status", path)


class TestCascadeFailure(unittest.TestCase):

    def test_direct_dependents_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
            ], path)
            update_status("a", "failed", path)
            blocked = cascade_failure("a", path)
            self.assertEqual(blocked, ["b"])
            order = load_order(path)
            self.assertEqual(order["groups"][0]["tickets"][1]["status"], "blocked")

    def test_transitive_cascade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
                {"uuid": "b", "slug": "second", "depends_on": ["a"]},
                {"uuid": "c", "slug": "third", "depends_on": ["b"]},
            ], path)
            update_status("a", "failed", path)
            blocked = cascade_failure("a", path)
            self.assertEqual(sorted(blocked), ["b", "c"])

    def test_no_cascade_across_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            register_group("g2", "origin2", [
                {"uuid": "x", "slug": "other", "depends_on": []},
            ], path)
            update_status("a", "failed", path)
            blocked = cascade_failure("a", path)
            self.assertEqual(blocked, [])
            order = load_order(path)
            self.assertEqual(order["groups"][1]["tickets"][0]["status"], "pending")

    def test_cascade_standalone_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("s1", "standalone", path)
            blocked = cascade_failure("s1", path)
            self.assertEqual(blocked, [])


class TestArchiveOrder(unittest.TestCase):

    def test_archive_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_order({"groups": [], "standalone": []}, path)
            archived = archive_order(path)
            self.assertIsNotNone(archived)
            self.assertTrue(archived.exists())
            self.assertFalse(path.exists())
            self.assertIn("usher_order_", archived.name)

    def test_archive_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            result = archive_order(path)
            self.assertIsNone(result)

    def test_archive_same_day_gets_time_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            # First archive
            save_order({"groups": [], "standalone": []}, path)
            first = archive_order(path)
            # Second archive same day
            save_order({"groups": [], "standalone": []}, path)
            second = archive_order(path)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)


class TestDetectStaleRunning(unittest.TestCase):

    def test_finds_running_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("u1", "ticket-1", path)
            register_standalone("u2", "ticket-2", path)
            update_status("u1", "running", path)
            stale = detect_stale_running(path)
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["uuid"], "u1")

    def test_no_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_standalone("u1", "ticket-1", path)
            stale = detect_stale_running(path)
            self.assertEqual(stale, [])

    def test_running_in_group_includes_group_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            register_group("g1", "origin", [
                {"uuid": "a", "slug": "first", "depends_on": []},
            ], path)
            update_status("a", "running", path)
            stale = detect_stale_running(path)
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["group_id"], "g1")


if __name__ == "__main__":
    unittest.main()
