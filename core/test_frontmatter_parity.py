"""Parity tests for every frontmatter reader in the repo (Phase 3.3).

Five subsystems once carried five hand-rolled ``---`` parsers
(``docs/review/subsystems/knowledge.md`` §3). Nothing failed when they drifted:
a learning's ``tags: [a, b]`` came back from researcher's reader as the *string*
``'[a, b]'``, and a research entry's block list came back from scribe's as
``''``. Two files that were both "markdown with YAML frontmatter" could not be
read by each other's tool, and no test noticed.

They all call ``core.frontmatter`` now. These tests are what keep that true:
one document, six readers, one answer. Model: ``core/test_secret_patterns.py``.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from captures import store as captures_store  # noqa: E402
from core import context_rules, frontmatter  # noqa: E402
from docs import check as docs_check  # noqa: E402
from researcher import store as researcher_store  # noqa: E402
from scribe import templates as scribe_templates  # noqa: E402
from scribe import store as scribe_store  # noqa: E402

# One document exercising every dialect feature, written the way each subsystem
# writes its own files. Every reader below must return exactly EXPECTED_META.
PARITY_DOC = """\
---
title: "Claude Code GUI: interactive-wrapper vs Agent-SDK billing"
topic: gui
tags: [gui, claude-code, billing]
areas: [ideas/*/0[0-9]-*.md, "**/*.py"]
required: [What was done, Key decisions, What's pending, Where it stopped]
sources:
  - https://example.com/a#frag
  - http://h:8080/p
supersedes: []
metadata:
  type: reference
  version: "1.0"
---
# Body

Left alone, --- and all.
"""

EXPECTED_META = {
    "title": "Claude Code GUI: interactive-wrapper vs Agent-SDK billing",
    "topic": "gui",
    "tags": ["gui", "claude-code", "billing"],
    "areas": ["ideas/*/0[0-9]-*.md", "**/*.py"],
    "required": [
        "What was done",
        "Key decisions",
        "What's pending",
        "Where it stopped",
    ],
    "sources": ["https://example.com/a#frag", "http://h:8080/p"],
    "supersedes": [],
    "metadata": {"type": "reference", "version": "1.0"},
}

EXPECTED_BODY = "# Body\n\nLeft alone, --- and all.\n"


class ParityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str = PARITY_DOC) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path


class TestEveryReaderAgrees(ParityTestCase):
    """Same bytes in, same ``(meta, body)`` out, whichever subsystem reads them."""

    def test_core_frontmatter(self) -> None:
        self.assertEqual(frontmatter.parse(PARITY_DOC), (EXPECTED_META, EXPECTED_BODY))

    def test_scribe_learning(self) -> None:
        meta, body = scribe_store._parse_learning_content(PARITY_DOC)
        self.assertEqual((meta, body), (EXPECTED_META, EXPECTED_BODY))

    def test_researcher_entry(self) -> None:
        meta, body = researcher_store.parse_entry(self.write("entry.md"))
        self.assertEqual((meta, body), (EXPECTED_META, EXPECTED_BODY))

    def test_captures_sidecar(self) -> None:
        meta, body = captures_store.parse_sidecar(self.write("sidecar.md"))
        self.assertEqual((meta, body), (EXPECTED_META, EXPECTED_BODY))

    def test_context_rule(self) -> None:
        # ``context_rules`` normalizes the body's trailing newlines by design
        # (the body is hashed); the meta must still match exactly.
        meta, body = context_rules._parse_frontmatter(PARITY_DOC, self.tmp / "rule.md")
        self.assertEqual(meta, EXPECTED_META)
        self.assertEqual(body, EXPECTED_BODY)

    def test_docs_checker(self) -> None:
        self.assertEqual(docs_check.parse_frontmatter(self.write("doc.md")), EXPECTED_META)

    def test_scribe_template_gate(self) -> None:
        self.assertEqual(
            scribe_templates.required_sections(PARITY_DOC),
            EXPECTED_META["required"],
        )


class TestCrossSubsystemReadWrite(ParityTestCase):
    """A file one subsystem writes is readable by every other one.

    This is the property that did not hold before Phase 3.3 — the probe in
    knowledge.md §3 is exactly this test, and it failed.
    """

    #: The list styles the writers use. Both must read back identically.
    STYLES = ("block", "inline")

    def _read_every_way(self, path: Path) -> list[dict]:
        text = path.read_text(encoding="utf-8")
        return [
            frontmatter.parse(text)[0],
            scribe_store._parse_learning_content(text)[0],
            researcher_store.parse_entry(path)[0],
            captures_store.parse_sidecar(path)[0],
            context_rules._parse_frontmatter(text, path)[0],
            docs_check.parse_frontmatter(path),
        ]

    def test_researcher_writes_scribe_reads(self) -> None:
        path = self.tmp / "r.md"
        researcher_store.write_entry(path, EXPECTED_META, EXPECTED_BODY)
        for meta in self._read_every_way(path):
            self.assertEqual(meta, EXPECTED_META)

    def test_captures_writes_scribe_reads(self) -> None:
        path = self.tmp / "c.md"
        captures_store.write_sidecar(path, EXPECTED_META, EXPECTED_BODY)
        for meta in self._read_every_way(path):
            self.assertEqual(meta, EXPECTED_META)

    def test_scribe_writes_researcher_reads(self) -> None:
        """Scribe emits inline lists; researcher's reader must cope."""
        path = self.tmp / "s.md"
        learning_meta = {
            "tags": ["scribe", "frontmatter"],
            "areas": ["scribe/store.py", "ideas/*/0[0-9]-*.md"],
            "supersedes": ["L-2026-108"],
        }
        path.write_text(
            scribe_store._format_learning_content(EXPECTED_BODY, learning_meta),
            encoding="utf-8",
        )
        for meta in self._read_every_way(path):
            self.assertEqual(meta, learning_meta)

    def test_both_list_styles_read_back_the_same(self) -> None:
        metas = []
        for style in self.STYLES:
            path = self.tmp / f"{style}.md"
            path.write_text(
                frontmatter.dump(EXPECTED_META, EXPECTED_BODY, list_style=style),
                encoding="utf-8",
            )
            metas.extend(self._read_every_way(path))
        for meta in metas:
            self.assertEqual(meta, EXPECTED_META)


class TestNoSecondDialect(unittest.TestCase):
    """§5a-C: one ``core/frontmatter.py`` and no other copies of the dialect.

    The doc-only rule ("Reuse core/") demonstrably failed — five parsers grew
    anyway. This is the mechanical version of it: a module that scans for
    ``---`` fences by hand is writing a sixth dialect, and this test says so
    before it ships.
    """

    #: Source markers of a hand-rolled fence scan.
    FENCE_SCANS = (
        '== "---"',
        "== '---'",
        'startswith("---',
        "startswith('---",
        'r"^---',
        "r'^---",
    )

    #: The dialect's own module, its tests, and the migration script's frozen
    #: legacy copies (kept deliberately, to compare against).
    ALLOWED = {
        "core/frontmatter.py",
        "core/test_frontmatter.py",
        "core/test_frontmatter_parity.py",
        "scripts/migrate_frontmatter.py",
        "scripts/test_migrate_frontmatter.py",
        # Skips a block without parsing it; owned by the hook dispatcher work
        # (Phase 3.1), which is where it should be folded in.
        "core/hooks/startup_prompt_hook.py",
    }

    SKIP_PREFIXES = (".repos/", ".venv/", "build/", "dist/", ".claude/")

    #: Every module that reads frontmatter must get it from ``core``.
    DELEGATORS = (
        "scribe/store.py",
        "scribe/templates.py",   # the template gate; was in scribe/notes.py
        "researcher/store.py",
        "researcher/cli.py",
        "captures/store.py",
        "captures/cli.py",
        "core/context_rules.py",
        "docs/check.py",
    )

    def test_no_module_scans_fences_by_hand(self) -> None:
        offenders = []
        for path in sorted(REPO_ROOT.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in self.ALLOWED or rel.startswith(self.SKIP_PREFIXES):
                continue
            # Tests assert *about* the on-disk shape; they are not parsers.
            if path.name.startswith("test_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # pragma: no cover
                continue
            for marker in self.FENCE_SCANS:
                if marker in text:
                    offenders.append(f"{rel}: {marker}")
        self.assertEqual(offenders, [], f"hand-rolled fence scan(s): {offenders}")

    def test_every_reader_delegates_to_core(self) -> None:
        for rel in self.DELEGATORS:
            with self.subTest(module=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertIn(
                    "frontmatter",
                    text,
                    f"{rel} no longer references core.frontmatter",
                )
                self.assertTrue(
                    "from core import frontmatter" in text
                    or "from core import frontmatter as fm_lib" in text,
                    f"{rel} must import the dialect from core",
                )

    def test_yaml_mini_is_gone(self) -> None:
        self.assertFalse((REPO_ROOT / "researcher" / "_yaml_mini.py").exists())


if __name__ == "__main__":
    unittest.main()
