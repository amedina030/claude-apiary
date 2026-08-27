#!/usr/bin/env python3
"""Concurrency tests for scribe/store.py — the lost-update race.

Review §3 bug 4: `update_note`, `archive_note`, `unarchive_note`,
`archive_learning` and `remove_learning` read the index *outside* the
FileLock and then wrote the whole list back under it. Interleaving:

    A reads [T1, T2]
    B appends T3 (locked, atomic)
    A writes [T1', T2] (locked, atomic) -> T3's row is gone

The body file survives, so `repair` can resurrect the row with an empty
session and an mtime timestamp — but only if someone notices. Two sessions
running /wrapup at once is a realistic trigger: each `add` runs the retention
sweep, which archives the *other* session's handoff.

These tests are stress tests by necessity — the window is real time, not a
patchable seam. Each ran red on the pre-fix store (entries were lost on every
attempt) and green after.
"""
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe.store import INDEX_FILENAME, ScribeStore


def _run_all(targets):
    """Start every callable on its own thread, join, and re-raise the first error."""
    errors = []

    def guarded(fn, *a):
        try:
            fn(*a)
        except Exception as e:          # noqa: BLE001 — surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=guarded, args=(fn, *args))
               for fn, *args in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f'worker raised: {errors[0]!r}'


class LostUpdateTests(unittest.TestCase):

    ADDS_PER_THREAD = 25
    THREADS = 4

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.store = ScribeStore(self.tmp_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _active_seqs(self, note_type='todo'):
        year_dir = next((self.tmp_dir / 'todos').iterdir())
        return {e['seq'] for e in ScribeStore._read_index(year_dir)}

    def test_updates_do_not_drop_concurrent_adds(self):
        anchor = self.store.add_note('todo', 'the note being updated', 's0')
        added = []
        lock = threading.Lock()

        def adder(worker):
            for i in range(self.ADDS_PER_THREAD):
                entry = self.store.add_note('todo', f'w{worker} n{i}', 'adder')
                with lock:
                    added.append(entry['seq'])

        def updater(worker):
            for i in range(self.ADDS_PER_THREAD):
                self.store.update_note('todo', anchor['year'], anchor['seq'],
                                       brief_summary=f'w{worker} pass {i}')

        _run_all([(adder, w) for w in range(self.THREADS)]
                 + [(updater, w) for w in range(self.THREADS)])

        expected = set(added) | {anchor['seq']}
        missing = expected - self._active_seqs()
        self.assertEqual(missing, set(),
                         f'{len(missing)} index row(s) lost to a concurrent update')
        self.assertEqual(len(added), self.THREADS * self.ADDS_PER_THREAD)
        self.assertEqual(len(set(added)), len(added), 'seqs must be unique')

    def test_archiving_does_not_drop_concurrent_adds(self):
        # The /wrapup collision: one session's retention sweep archives while
        # another session is adding.
        victims = [self.store.add_note('todo', f'archive me {i}', 's0')
                   for i in range(self.THREADS * 4)]
        added = []
        lock = threading.Lock()

        def adder(worker):
            for i in range(self.ADDS_PER_THREAD):
                entry = self.store.add_note('todo', f'w{worker} n{i}', 'adder')
                with lock:
                    added.append(entry['seq'])

        def archiver(worker):
            for victim in victims[worker::self.THREADS]:
                self.store.archive_note('todo', victim['year'], victim['seq'])

        _run_all([(adder, w) for w in range(self.THREADS)]
                 + [(archiver, w) for w in range(self.THREADS)])

        active = self._active_seqs()
        missing = set(added) - active
        self.assertEqual(missing, set(),
                         f'{len(missing)} index row(s) lost to a concurrent archive')
        # And every archived note landed in the archive index, exactly once.
        archived = [e['seq'] for e in self.store.list_notes(status='archived')]
        self.assertEqual(sorted(archived), sorted(v['seq'] for v in victims))

    def test_learning_writes_survive_concurrent_archive(self):
        victims = [self.store.add_learning(f'archive me {i}', 's0')
                   for i in range(self.THREADS * 4)]
        added = []
        lock = threading.Lock()

        def adder(worker):
            for i in range(self.ADDS_PER_THREAD):
                entry = self.store.add_learning(f'w{worker} n{i}', 'adder')
                with lock:
                    added.append(entry['seq'])

        def archiver(worker):
            for victim in victims[worker::self.THREADS]:
                self.store.archive_learning(victim['year'], victim['seq'])

        _run_all([(adder, w) for w in range(self.THREADS)]
                 + [(archiver, w) for w in range(self.THREADS)])

        active = {e['seq'] for e in self.store.list_learnings(status='active')}
        self.assertEqual(set(added) - active, set(),
                         'learning index row lost to a concurrent archive')


_WORKER = textwrap.dedent('''
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from scribe.store import ScribeStore
    store = ScribeStore(Path(sys.argv[2]))
    tag = sys.argv[3]
    for i in range(int(sys.argv[4])):
        store.add_note('todo', f'{tag} note {i}', tag)
''')


class TwoProcessAppendTests(unittest.TestCase):
    """The same race across processes, where the FileLock actually earns its keep.

    Threads share the GIL and a single interpreter; two `/wrapup`s in two
    terminals do not. This is the shape the review measured.
    """

    NOTES_PER_PROCESS = 30

    def test_two_processes_appending_lose_nothing(self):
        repo_root = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            ScribeStore(Path(tmp))  # create the layout before the race starts
            procs = [
                subprocess.Popen(
                    [sys.executable, '-c', _WORKER, repo_root, tmp, tag,
                     str(self.NOTES_PER_PROCESS)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding='utf-8')
                for tag in ('alpha', 'beta')
            ]
            for proc in procs:
                _, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err)

            store = ScribeStore(Path(tmp))
            rows = store.list_notes(note_type='todo', status='active')
            self.assertEqual(len(rows), 2 * self.NOTES_PER_PROCESS,
                             'an index row was lost between the two processes')
            self.assertEqual(len({r['seq'] for r in rows}), len(rows),
                             'two processes were issued the same seq')
            # Every row has the body file it claims, and vice versa.
            year_dir = next((Path(tmp) / 'todos').iterdir())
            bodies = {int(p.stem) for p in year_dir.glob('*.md')}
            self.assertEqual(bodies, {r['seq'] for r in rows})
            self.assertNotIn('', (year_dir / INDEX_FILENAME).read_text(
                encoding='utf-8').splitlines(), 'no blank lines in the index')


if __name__ == '__main__':
    unittest.main()
