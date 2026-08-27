"""`apiary install` prunes commands apiary no longer ships — but only copies the
user never edited (hash still equals the one recorded at the previous install)."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from core.install import _copy_slash_commands


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class PruneRemovedCommandsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.apiary = root / "apiary"
        (self.apiary / "budgeter" / "commands").mkdir(parents=True)
        (self.apiary / "budgeter" / "commands" / "budgeter.md").write_text("# new\n", encoding="utf-8")
        self.target = root / "target"
        self.dest = self.target / ".claude" / "commands"
        self.dest.mkdir(parents=True)
        # Previous install shipped budgeter-log.md; the user edited budgeter-warn.md;
        # mine.md is the user's own file, never recorded.
        (self.dest / "budgeter-log.md").write_text("# old\n", encoding="utf-8")
        (self.dest / "budgeter-warn.md").write_text("# old warn\n", encoding="utf-8")
        (self.dest / "mine.md").write_text("# mine\n", encoding="utf-8")
        self.previous = {
            "budgeter-log.md": _sha(self.dest / "budgeter-log.md"),
            "budgeter-warn.md": "0" * 64,               # recorded hash != current -> edited
        }

    def test_unmodified_stale_removed_edited_and_unrecorded_kept(self):
        hashes = _copy_slash_commands(self.target, self.apiary, self.previous)
        self.assertEqual(set(hashes), {"budgeter.md"})
        self.assertFalse((self.dest / "budgeter-log.md").exists(), "unmodified stale copy pruned")
        self.assertTrue((self.dest / "budgeter-warn.md").exists(), "edited copy kept")
        self.assertTrue((self.dest / "mine.md").exists(), "user's own file untouched")
        self.assertTrue((self.dest / "budgeter.md").exists())

    def test_no_previous_record_prunes_nothing(self):
        _copy_slash_commands(self.target, self.apiary, None)
        self.assertTrue((self.dest / "budgeter-log.md").exists())


if __name__ == "__main__":
    unittest.main()
