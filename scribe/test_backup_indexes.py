"""Tests for scribe/backup_indexes.py."""
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe.store import ScribeStore, TYPE_FOLDERS, LEARNING_FOLDER, INDEX_FILENAME
from scribe import backup_indexes


class BackupIndexesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / 'scribe'
        self.store = ScribeStore(self.state_dir)
        self.store.add_note('todo', 'test note one', 'sess1')
        self.store.add_note('handoff', 'test handoff note', 'sess1')
        self.store.add_learning('test learning entry', 'sess1')

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_backup_copies_all_indexes(self):
        backups_root = self.state_dir / 'backups'
        target, count = backup_indexes.create_backup(self.state_dir, backups_root, '2026-04-10')

        self.assertTrue(target.exists())
        year = str(datetime.now(timezone.utc).year)
        # Year-layout indexes for types with data (todo, handoff, learning)
        for folder_name in ('todos', 'handoffs'):
            self.assertTrue(
                (target / folder_name / year / INDEX_FILENAME).exists(),
                f'Missing index for folder {folder_name}/{year}',
            )
        self.assertTrue((target / LEARNING_FOLDER / year / INDEX_FILENAME).exists())
        # Legacy next_id must NOT appear in backup
        self.assertFalse((target / 'next_id').exists())
        self.assertGreater(count, 0)

    def test_backup_idempotent_same_day(self):
        backups_root = self.state_dir / 'backups'

        target, _ = backup_indexes.create_backup(self.state_dir, backups_root, '2026-04-10')
        self.assertTrue(target.exists())

        # Write a sentinel file into the target
        sentinel = target / 'sentinel.txt'
        sentinel.write_text('should be gone after second backup', encoding='utf-8')
        self.assertTrue(sentinel.exists())

        # Second call should overwrite — no exception, sentinel gone
        target2, _ = backup_indexes.create_backup(self.state_dir, backups_root, '2026-04-10')
        self.assertEqual(target, target2)
        self.assertTrue(target2.exists())
        self.assertFalse(sentinel.exists(), 'Sentinel should have been removed by overwrite')

        year = str(datetime.now(timezone.utc).year)
        for folder_name in ('todos', 'handoffs'):
            self.assertTrue((target2 / folder_name / year / INDEX_FILENAME).exists())
        self.assertTrue((target2 / LEARNING_FOLDER / year / INDEX_FILENAME).exists())

    def test_prune_retains_newest_n(self):
        backups_root = self.state_dir / 'backups'
        backups_root.mkdir(parents=True, exist_ok=True)

        date_names = [f'2026-01-{i:02d}' for i in range(1, 11)]  # 01 through 10
        for name in date_names:
            (backups_root / name).mkdir()

        backup_indexes.prune_backups(backups_root, retain=3)

        remaining = sorted(d.name for d in backups_root.iterdir() if d.is_dir())
        self.assertEqual(remaining, ['2026-01-08', '2026-01-09', '2026-01-10'])

    def test_prune_retain_zero_keeps_only_latest(self):
        backups_root = self.state_dir / 'backups'
        backups_root.mkdir(parents=True, exist_ok=True)

        (backups_root / '2026-01-01').mkdir()
        (backups_root / '2026-01-02').mkdir()

        backup_indexes.prune_backups(backups_root, retain=0)

        remaining = sorted(d.name for d in backups_root.iterdir() if d.is_dir())
        self.assertEqual(remaining, ['2026-01-02'])

    def test_prune_no_op_when_below_retain(self):
        backups_root = self.state_dir / 'backups'
        backups_root.mkdir(parents=True, exist_ok=True)

        (backups_root / '2026-01-01').mkdir()
        (backups_root / '2026-01-02').mkdir()

        backup_indexes.prune_backups(backups_root, retain=5)

        remaining = sorted(d.name for d in backups_root.iterdir() if d.is_dir())
        self.assertEqual(remaining, ['2026-01-01', '2026-01-02'])

    def test_main_end_to_end(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        test_argv = ['backup_indexes.py', '--retain', '5']

        with unittest.mock.patch('sys.argv', test_argv), \
             unittest.mock.patch.object(backup_indexes, 'resolve_state_dir', return_value=self.state_dir):
            result = backup_indexes.main()

        self.assertEqual(result, 0)

        backups_root = self.state_dir / 'backups'
        today_dir = backups_root / today
        self.assertTrue(today_dir.exists(), f'Expected backup dir {today_dir} to exist')
        year = str(datetime.now(timezone.utc).year)
        for folder_name in ('todos', 'handoffs'):
            self.assertTrue((today_dir / folder_name / year / INDEX_FILENAME).exists())
        self.assertTrue((today_dir / LEARNING_FOLDER / year / INDEX_FILENAME).exists())


if __name__ == '__main__':
    unittest.main()
