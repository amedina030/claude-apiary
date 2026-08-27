#!/usr/bin/env python3
"""Tests for core/context_rules.py and the source files under context-rules/.

Two layers of coverage here:

1. **Source-file conformance (Layer 4 of #224)** — every file under
   context-rules/**/*.md must parse cleanly, have valid required frontmatter
   fields, and have id/category that match filename + parent directory.

2. **Library unit tests** — frontmatter parsing, hash determinism, render +
   parse round-trip, drift detection, zone tamper detection.
"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr  # noqa: E402

# ---------------------------------------------------------------------------
# Layer 4 — source files conform
# ---------------------------------------------------------------------------


class TestSourceFiles(unittest.TestCase):
    def test_all_rules_load(self):
        rules = cr.load_all_rules()
        self.assertGreaterEqual(len(rules), 1, "expected at least 1 rule in repo")
        ids = [r.id for r in rules]
        self.assertEqual(len(ids), len(set(ids)), "duplicate rule ids")

    def test_required_rules_present(self):
        ids = {r.id for r in cr.load_all_rules()}
        self.assertIn("load_apiary_context", ids)

    def test_render_is_deterministic(self):
        rules = cr.load_all_rules()
        z1 = cr.render_managed_zone(rules)
        z2 = cr.render_managed_zone(rules)
        self.assertEqual(z1, z2)
        self.assertEqual(
            hashlib.sha256(z1.encode("utf-8")).hexdigest(),
            hashlib.sha256(z2.encode("utf-8")).hexdigest(),
        )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestFrontmatter(unittest.TestCase):
    def _write(self, body: str) -> Path:
        d = Path(self._tmp.name) / "behavioral"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "test_rule.md"
        p.write_text(body, encoding="utf-8")
        return p

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_minimal_valid(self):
        p = self._write(
            "---\nid: test_rule\ntitle: T\ncategory: behavioral\nrequires: []\n---\nBody.\n"
        )
        r = cr.load_rule(p)
        self.assertEqual(r.id, "test_rule")
        self.assertEqual(r.category, "behavioral")
        self.assertEqual(r.requires, ())
        self.assertEqual(r.body.strip(), "Body.")

    def test_id_mismatch_filename(self):
        p = self._write(
            "---\nid: wrong\ntitle: T\ncategory: behavioral\nrequires: []\n---\nBody.\n"
        )
        with self.assertRaises(cr.RuleParseError):
            cr.load_rule(p)

    def test_category_mismatch_dir(self):
        p = self._write(
            "---\nid: test_rule\ntitle: T\ncategory: apiary-tools\nrequires: []\n---\nBody.\n"
        )
        with self.assertRaises(cr.RuleParseError):
            cr.load_rule(p)

    def test_missing_field(self):
        p = self._write("---\nid: test_rule\ncategory: behavioral\nrequires: []\n---\nBody.\n")
        with self.assertRaises(cr.RuleParseError):
            cr.load_rule(p)

    def test_missing_closing_fence(self):
        p = self._write(
            "---\nid: test_rule\ntitle: T\ncategory: behavioral\nrequires: []\nBody no fence\n"
        )
        with self.assertRaises(cr.RuleParseError):
            cr.load_rule(p)

    def test_empty_body(self):
        p = self._write("---\nid: test_rule\ntitle: T\ncategory: behavioral\nrequires: []\n---\n\n")
        with self.assertRaises(cr.RuleParseError):
            cr.load_rule(p)

    def test_requires_list(self):
        p = self._write(
            "---\nid: test_rule\ntitle: T\ncategory: behavioral\nrequires: [a, b]\n---\nBody.\n"
        )
        r = cr.load_rule(p)
        self.assertEqual(r.requires, ("a", "b"))


# ---------------------------------------------------------------------------
# Render + parse round-trip
# ---------------------------------------------------------------------------


def _make_rule(rid: str, body: str) -> cr.Rule:
    return cr.Rule(
        id=rid,
        title=rid,
        category="behavioral",
        requires=(),
        body=body if body.endswith("\n") else body + "\n",
        source_path=Path(f"/tmp/{rid}.md"),
    )


class TestRenderRoundTrip(unittest.TestCase):
    def test_render_parse_roundtrip(self):
        rules = [_make_rule("a", "Body A."), _make_rule("b", "Body B.")]
        zone = cr.render_managed_zone(rules)
        parsed = cr.find_managed_zone(zone)
        self.assertIsNotNone(parsed)
        self.assertEqual([ir.id for ir in parsed.rules], ["a", "b"])
        for ir, r in zip(parsed.rules, rules):
            self.assertEqual(ir.stored_hash, r.body_hash)
            self.assertEqual(ir.body_hash, r.body_hash)
        self.assertEqual(parsed.stray_text, "")

    def test_outer_content_preserved_when_zone_replaced(self):
        prefix = "# My personal notes\n\nSome stuff.\n\n"
        suffix = "# More notes\n\nFinal stuff.\n"
        rules = [_make_rule("a", "Body A.")]
        zone = cr.render_managed_zone(rules)
        full = prefix + zone + suffix
        parsed = cr.find_managed_zone(full)
        self.assertEqual(full[: parsed.start], prefix)
        # parsed.end stops after OUTER_END text, leaving the trailing newline
        # of the zone in the "after" slice. That's expected.
        self.assertEqual(full[parsed.end :], "\n" + suffix)


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection(unittest.TestCase):
    def test_no_zone_returns_none(self):
        self.assertIsNone(cr.find_managed_zone("# just notes\nnothing here\n"))

    def test_duplicate_start_marker(self):
        text = cr.OUTER_START + "\n" + cr.OUTER_START + "\n" + cr.OUTER_END + "\n"
        with self.assertRaises(cr.ZoneTamperError):
            cr.find_managed_zone(text)

    def test_missing_end_marker(self):
        text = cr.OUTER_START + "\nstuff\n"
        with self.assertRaises(cr.ZoneTamperError):
            cr.find_managed_zone(text)

    def test_end_before_start(self):
        text = cr.OUTER_END + "\n\n" + cr.OUTER_START + "\n"
        with self.assertRaises(cr.ZoneTamperError):
            cr.find_managed_zone(text)

    def test_stray_text_inside_zone(self):
        # Hand-craft a zone with junk text not enclosed in inner sentinels.
        text = cr.OUTER_START + "\n\nthis is not a rule\n\n" + cr.OUTER_END + "\n"
        zone = cr.find_managed_zone(text)
        self.assertTrue(zone.stray_text)

    def test_body_tampering_detected(self):
        rule = _make_rule("a", "Body A.")
        zone = cr.render_managed_zone([rule])
        tampered = zone.replace("Body A.", "Body A MODIFIED.")
        parsed = cr.find_managed_zone(tampered)
        ir = parsed.rules[0]
        self.assertNotEqual(ir.body_hash, ir.stored_hash)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


class TestDrift(unittest.TestCase):
    def test_clean(self):
        rules = [_make_rule("a", "Body A."), _make_rule("b", "Body B.")]
        zone = cr.render_managed_zone(rules)
        report = cr.compute_drift(zone, rules)
        self.assertTrue(report.clean)
        self.assertTrue(report.zone_present)

    def test_no_zone_is_clean(self):
        rules = [_make_rule("a", "Body A.")]
        report = cr.compute_drift("# nothing here\n", rules)
        self.assertTrue(report.clean)
        self.assertFalse(report.zone_present)

    def test_source_changed(self):
        old = _make_rule("a", "Body OLD.")
        zone = cr.render_managed_zone([old])
        new = _make_rule("a", "Body NEW.")
        report = cr.compute_drift(zone, [new])
        self.assertEqual(report.hash_mismatch, ["a"])
        self.assertEqual(report.tampered_bodies, [])

    def test_body_tampered(self):
        rule = _make_rule("a", "Body A.")
        zone = cr.render_managed_zone([rule])
        tampered = zone.replace("Body A.", "Body MODIFIED.")
        report = cr.compute_drift(tampered, [rule])
        self.assertEqual(report.tampered_bodies, ["a"])

    def test_missing_source(self):
        rule = _make_rule("a", "Body A.")
        zone = cr.render_managed_zone([rule])
        # Source list does not contain 'a' anymore.
        report = cr.compute_drift(zone, [])
        self.assertEqual(report.missing_source, ["a"])

    def test_not_installed(self):
        rules = [_make_rule("a", "Body A."), _make_rule("b", "Body B.")]
        zone = cr.render_managed_zone([rules[0]])
        report = cr.compute_drift(zone, rules)
        self.assertEqual(report.not_installed, ["b"])

    def test_zone_tamper_marks_stray(self):
        # Two start markers => tamper => report.stray_text True
        text = cr.OUTER_START + "\n" + cr.OUTER_START + "\n" + cr.OUTER_END + "\n"
        report = cr.compute_drift(text, [])
        self.assertTrue(report.stray_text)


if __name__ == "__main__":
    unittest.main()
