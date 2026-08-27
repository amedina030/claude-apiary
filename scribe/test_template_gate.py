"""Template gate (deep review §5a-B, option C): required sections, one attempt.

A template whose frontmatter declares ``required:`` rejects content missing a
section; a template without ``required:`` is guidance and never blocks; a type
with no template accepts anything; ``--force`` bypasses and says so. There is
no hash to acknowledge — that flow was deleted.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe import notes as notes_mod
from scribe import templates as templates_mod
from scribe.store import VALID_TYPES, ScribeStore

HANDOFF_OK = ("## Session abcd1234 Handoff\n\n### What was done\n- x\n\n"
              "### Key decisions\n- y\n\n### What's pending\n- z\n\n"
              "### Where it stopped\n- w\n")
HANDOFF_MISSING_PENDING = ("### What was done\n- x\n\n### Key decisions\n- y\n\n"
                           "### Where it stopped\n- w\n")


def _args(store, **over):
    base = dict(store=store, content=None, content_file=None, type="todo",
                summary="", brief_summary="", session_id="s", auto=False,
                if_no_handoff_for=None, role="", mission="",
                tags="", unique_tag="", force=False)
    base.update(over)
    return SimpleNamespace(**base)


class TestPureHelpers(unittest.TestCase):
    def test_required_sections_parse(self):
        tpl = "---\nrequired: [shipped, open, pick up next session with]\n---\nbody"
        self.assertEqual(templates_mod.required_sections(tpl),
                         ["shipped", "open", "pick up next session with"])

    def test_no_required_key(self):
        self.assertEqual(templates_mod.required_sections("---\nfoo: bar\n---\n"), [])

    def test_section_present_heading(self):
        self.assertTrue(templates_mod.section_present("## Shipped\n- x", "shipped"))

    def test_section_present_bold_label(self):
        self.assertTrue(templates_mod.section_present("**Why:** because", "why"))
        self.assertTrue(templates_mod.section_present("**Why** stuff", "why"))

    def test_section_absent(self):
        self.assertFalse(templates_mod.section_present("nothing relevant", "shipped"))

    def test_missing_required(self):
        self.assertEqual(templates_mod.missing_sections("## Shipped", ["shipped", "open"]),
                         ["open"])

    def test_hash_ack_helpers_are_gone(self):
        self.assertFalse(hasattr(notes_mod, "template_hash"))
        self.assertFalse(hasattr(notes_mod, "TEMPLATE_HASH_LEN"))


class TestBundledTemplates(unittest.TestCase):
    """The shipped defaults: one per type, required only where structure earns it."""

    REQUIRED = {
        "handoff": ["What was done", "Key decisions", "What's pending", "Where it stopped"],
        "decision": ["Context", "Decision", "Why", "Consequences"],
        "blocker": ["Blocked on", "Tried", "Unblock when"],
    }
    GUIDANCE_ONLY = ["todo", "wishlist", "reference", "context", "general"]

    def test_one_template_per_type(self):
        for note_type in VALID_TYPES:
            with self.subTest(note_type=note_type):
                self.assertTrue((templates_mod.DEFAULT_TEMPLATES_DIR / f"{note_type}.md").is_file())

    def test_required_sections_match_the_spec(self):
        for note_type, expected in self.REQUIRED.items():
            with self.subTest(note_type=note_type):
                text = (templates_mod.DEFAULT_TEMPLATES_DIR / f"{note_type}.md").read_text(
                    encoding="utf-8")
                self.assertEqual(templates_mod.required_sections(text), expected)

    def test_guidance_templates_declare_nothing(self):
        for note_type in self.GUIDANCE_ONLY:
            with self.subTest(note_type=note_type):
                text = (templates_mod.DEFAULT_TEMPLATES_DIR / f"{note_type}.md").read_text(
                    encoding="utf-8")
                self.assertEqual(templates_mod.required_sections(text), [])

    def test_templates_satisfy_their_own_required_sections(self):
        for note_type in self.REQUIRED:
            with self.subTest(note_type=note_type):
                text = (templates_mod.DEFAULT_TEMPLATES_DIR / f"{note_type}.md").read_text(
                    encoding="utf-8")
                self.assertEqual(
                    templates_mod.missing_sections(
                        text, templates_mod.required_sections(text)), [])

    def test_handoff_required_matches_wrapup_skill(self):
        """The gate must accept the handoff shape core/commands/wrapup.md prescribes."""
        wrapup = (Path(__file__).resolve().parent.parent
                  / "core" / "commands" / "wrapup.md").read_text(encoding="utf-8")
        for section in self.REQUIRED["handoff"]:
            with self.subTest(section=section):
                self.assertIn(f"### {section}", wrapup)


class TestScaffold(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_scaffold_writes_every_type(self):
        written = templates_mod.scaffold_defaults(self.state_dir)
        self.assertEqual(written, sorted(VALID_TYPES))
        for note_type in VALID_TYPES:
            self.assertTrue(templates_mod.template_path(self.state_dir, note_type).is_file())

    def test_scaffold_is_idempotent(self):
        templates_mod.scaffold_defaults(self.state_dir)
        self.assertEqual(templates_mod.scaffold_defaults(self.state_dir), [])

    def test_scaffold_never_overwrites_an_edited_template(self):
        path = templates_mod.template_path(self.state_dir, "handoff")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MINE\n", encoding="utf-8")
        written = templates_mod.scaffold_defaults(self.state_dir)
        self.assertNotIn("handoff", written)
        self.assertEqual(path.read_text(encoding="utf-8"), "MINE\n")


class TestGate(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))
        templates_mod.scaffold_defaults(self.store.state_dir)

    def tearDown(self):
        self._td.cleanup()

    def test_type_without_template_accepts_anything(self):
        templates_mod.template_path(self.store.state_dir, "todo").unlink()
        notes_mod.cmd_add(_args(self.store, type="todo", content="whatever"))
        self.assertEqual(len(self.store.list_notes(note_type="todo")), 1)

    def test_guidance_template_never_blocks(self):
        # todo ships a template with no `required:` — content is unconstrained.
        notes_mod.cmd_add(_args(self.store, type="todo", content="whatever"))
        self.assertEqual(len(self.store.list_notes(note_type="todo")), 1)

    def test_conforming_handoff_passes(self):
        notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_OK,
                                summary="s1"))
        self.assertEqual(len(self.store.list_notes(note_type="handoff")), 1)

    def test_missing_section_rejected_and_nothing_written(self):
        with self.assertRaises(SystemExit):
            notes_mod.cmd_add(_args(self.store, type="handoff",
                                    content=HANDOFF_MISSING_PENDING, summary="s1"))
        self.assertEqual(len(self.store.list_notes(note_type="handoff", status="all")), 0)

    def test_rejection_message_inlines_the_template(self):
        buf = io.StringIO()
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            notes_mod.cmd_add(_args(self.store, type="handoff",
                                    content=HANDOFF_MISSING_PENDING, summary="s1"))
        err = buf.getvalue()
        self.assertIn("What's pending", err)
        self.assertIn("--- handoff.md ---", err)
        self.assertIn("--force", err)

    def test_force_bypasses_and_logs(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            notes_mod.cmd_add(_args(self.store, type="handoff",
                                    content=HANDOFF_MISSING_PENDING, summary="s1",
                                    force=True))
        self.assertEqual(len(self.store.list_notes(note_type="handoff")), 1)
        self.assertIn("bypassed via --force", buf.getvalue())

    def test_force_is_silent_when_nothing_was_missing(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            notes_mod.cmd_add(_args(self.store, type="handoff", content=HANDOFF_OK,
                                    summary="s1", force=True))
        self.assertNotIn("--force", buf.getvalue())

    def test_gate_is_per_type(self):
        # The handoff template must not constrain a decision note, and vice versa.
        notes_mod.cmd_add(_args(self.store, type="decision",
                                content="### Context\n### Decision\n### Why\n### Consequences\n"))
        self.assertEqual(len(self.store.list_notes(note_type="decision")), 1)
        with self.assertRaises(SystemExit):
            notes_mod.cmd_add(_args(self.store, type="blocker", content=HANDOFF_OK))

    def test_gate_is_forward_only(self):
        """Existing notes are never validated or rewritten — only `add` checks."""
        entry = self.store.add_note("handoff", "free-form legacy handoff", "s0",
                                    summary="legacy")
        notes_mod.cmd_update(_args(self.store, id=entry["display_id"],
                                   content="still free-form", session_id=None,
                                   brief_summary="", add_tag=[], remove_tag=[]))
        got = self.store.get_note("handoff", entry["year"], entry["seq"])
        self.assertEqual(got["content"], "still free-form")


if __name__ == "__main__":
    unittest.main()
