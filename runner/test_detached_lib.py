#!/usr/bin/env python3
"""Unit tests for runner/detached_lib.py."""
import json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

# Ensure runner dir is on sys.path so detached_lib can be imported when
# this module is loaded as 'runner.test_detached_lib' via `python -m unittest`.
_RUNNER_DIR = Path(__file__).resolve().parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import detached_lib  # noqa: E402  (path manipulation above is intentional)

class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(detached_lib.slugify('Hello World!'), 'hello-world')
    def test_empty(self):
        self.assertEqual(detached_lib.slugify(''), 'item')
    def test_unicode(self):
        self.assertTrue(detached_lib.slugify('A B C').startswith('a-b'))

class TestShortUuid(unittest.TestCase):
    def test_length_and_hex(self):
        u = detached_lib.short_uuid()
        self.assertEqual(len(u), 8)
        int(u, 16)

class TestHygiene(unittest.TestCase):
    def test_ok_when_below_max(self):
        with mock.patch.object(detached_lib, 'list_unmerged_runner_branches', return_value=['runner/a-1', 'runner/b-2']):
            self.assertIsNone(detached_lib.hygiene_precheck(5))
    def test_full(self):
        with mock.patch.object(detached_lib, 'list_unmerged_runner_branches', return_value=['runner/a','runner/b','runner/c','runner/d','runner/e']):
            reason = detached_lib.hygiene_precheck(5)
            self.assertIn('queue full', reason)
            self.assertIn('5/5', reason)

class TestPickBacklog(unittest.TestCase):
    def test_picks_oldest_not_claimed(self):
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td) / 'backlog'
            bdir.mkdir()
            # create two items, write older one first
            a = bdir / 'a.json'
            b = bdir / 'b.json'
            a.write_text(json.dumps({'id': 'uuid-a', 'title': 'A'}), encoding='utf-8')
            b.write_text(json.dumps({'id': 'uuid-b', 'title': 'B'}), encoding='utf-8')
            import os, time
            os.utime(a, (1000, 1000))
            os.utime(b, (2000, 2000))
            with mock.patch.object(detached_lib, 'BACKLOG_DIR', bdir):
                with mock.patch.object(detached_lib, 'list_runner_branches', return_value=[]):
                    p = detached_lib.pick_backlog_item()
                    self.assertEqual(p.name, 'a.json')
                with mock.patch.object(detached_lib, 'list_runner_branches', return_value=['runner/a-xxxxxxxx-uuid-a']):
                    p = detached_lib.pick_backlog_item()
                    self.assertEqual(p.name, 'b.json')
    def test_empty(self):
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td) / 'backlog'
            bdir.mkdir()
            with mock.patch.object(detached_lib, 'BACKLOG_DIR', bdir):
                self.assertIsNone(detached_lib.pick_backlog_item())

class TestOvernightLog(unittest.TestCase):
    def test_append(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'overnight.jsonl'
            with mock.patch.object(detached_lib, 'OVERNIGHT_LOG', p):
                self.assertTrue(detached_lib.append_overnight_log({'a': 1}))
                self.assertTrue(detached_lib.append_overnight_log({'a': 2}))
            lines = p.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])['a'], 1)

if __name__ == '__main__':
    unittest.main()
