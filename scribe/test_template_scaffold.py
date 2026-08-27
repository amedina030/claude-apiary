"""scaffold_defaults refresh rules: bundled changes reach an unmodified
copy; a user-edited copy and a pre-existing UNRECORDED copy are never touched."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scribe import templates
from scribe.store import VALID_TYPES


class ScaffoldRefreshTest(unittest.TestCase):
    def test_unrecorded_preexisting_template_is_never_overwritten(self):
        # A state dir from before the hash record existed (pre-Phase-1 installs).
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            tdir = state / templates.TEMPLATES_DIRNAME
            tdir.mkdir()
            (tdir / "handoff.md").write_text("my old handoff template\n", encoding="utf-8")
            templates.scaffold_defaults(state)
            self.assertEqual((tdir / "handoff.md").read_text(encoding="utf-8"), "my old handoff template\n")
            # Other types were scaffolded and recorded.
            self.assertTrue((tdir / "decision.md").exists())
            record = json.loads((tdir / ".bundled_hashes.json").read_text(encoding="utf-8"))
            self.assertIn("decision", record)
            self.assertNotIn("handoff", record)

    def test_unmodified_copy_refreshes_when_bundled_changes_and_edited_copy_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            templates.scaffold_defaults(state)
            tdir = state / templates.TEMPLATES_DIRNAME
            record = json.loads((tdir / ".bundled_hashes.json").read_text(encoding="utf-8"))
            # Simulate an older bundled version: the installed copy differs from
            # today's bundle while its RECORDED hash matches it (we scaffolded it).
            old = "older bundled decision template\n"
            (tdir / "decision.md").write_text(old, encoding="utf-8")
            record["decision"] = hashlib.sha256(old.encode("utf-8")).hexdigest()
            (tdir / ".bundled_hashes.json").write_text(json.dumps(record), encoding="utf-8")
            # An edited template: content differs from the recorded hash.
            (tdir / "blocker.md").write_text("user edited\n", encoding="utf-8")
            templates.scaffold_defaults(state)
            bundled = (templates.DEFAULT_TEMPLATES_DIR / "decision.md").read_text(encoding="utf-8")
            self.assertEqual((tdir / "decision.md").read_text(encoding="utf-8"), bundled)
            self.assertEqual((tdir / "blocker.md").read_text(encoding="utf-8"), "user edited\n")


if __name__ == "__main__":
    unittest.main()
