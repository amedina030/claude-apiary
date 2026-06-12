"""Layered template gate: hash-ack (T-2026-212) + required-sections (spec §5.8).

A template with a ``required:`` list rejects content missing a section; --force
bypasses both the ack and the section check; a type with no template accepts
anything. The hash-ack behavior is preserved unchanged.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore
from scribe import notes as notes_mod

HANDOFF_OK = "## Shipped\n- did x\n\n## Open\n- y\n\n## Pick up next session with\n- z\n"
HANDOFF_MISSING_OPEN = "## Shipped\n- did x\n\n## Pick up next session with\n- z\n"


def _args(store, **over):
    base = dict(store=store, content=None, content_file=None, type="todo",
                summary="", brief_summary="", session_id="s", auto=False,
                if_no_handoff_for=None, role="", mission="", ack_template="",
                tags="", unique_tag="", force=False)
    base.update(over)
    return SimpleNamespace(**base)


class TestPureHelpers(unittest.TestCase):
    def test_required_sections_parse(self):
        tpl = "---\nrequired: [shipped, open, pick up next session with]\n---\nbody"
        self.assertEqual(notes_mod.template_required_sections(tpl),
                         ["shipped", "open", "pick up next session with"])

    def test_no_required_key(self):
        self.assertEqual(notes_mod.template_required_sections("---\nfoo: bar\n---\n"), [])

    def test_section_present_heading(self):
        self.assertTrue(notes_mod._section_present("## Shipped\n- x", "shipped"))

    def test_section_present_bold_label(self):
        self.assertTrue(notes_mod._section_present("**Why:** because", "why"))
        self.assertTrue(notes_mod._section_present("**Why** stuff", "why"))

    def test_section_absent(self):
        self.assertFalse(notes_mod._section_present("nothing relevant", "shipped"))

    def test_missing_required(self):
        self.assertEqual(notes_mod.missing_required_sections("## Shipped", ["shipped", "open"]), ["open"])


class TestGateLayering(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))
        self.assertTrue(notes_mod.scaffold_handoff_template(self.store.state_dir))
        self.tpl_hash = notes_mod.template_hash(notes_mod.template_text(self.store.state_dir, "handoff"))

    def tearDown(self):
        self._td.cleanup()

    def test_type_without_template_accepts_anything(self):
        notes_mod.cmd_add(_args(self.store, type="todo", content="whatever"))  # no SystemExit

    def test_conforming_handoff_with_ack_passes(self):
        notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_OK,
                                summary="s1", ack_template=self.tpl_hash))

    def test_missing_section_rejected(self):
        with self.assertRaises(SystemExit):
            notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_MISSING_OPEN,
                                    summary="s1", ack_template=self.tpl_hash))

    def test_force_bypasses_both(self):
        notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_MISSING_OPEN,
                                summary="s1", force=True))  # no SystemExit despite missing section + no ack

    def test_wrong_ack_rejected(self):
        with self.assertRaises(SystemExit):
            notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_OK,
                                    summary="s1", ack_template="wronghash"))

    def test_scaffold_is_idempotent(self):
        self.assertFalse(notes_mod.scaffold_handoff_template(self.store.state_dir))


if __name__ == "__main__":
    unittest.main()
