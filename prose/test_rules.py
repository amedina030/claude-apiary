"""Tests for the shipped rule set: every rule loads, fires on the tell fixture, and stays quiet on the clean one."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import engine

TELLS = engine.STYLES_DIR / "Tells"
FIXTURES = TELLS / "fixtures"


def shipped_config() -> dict:
    cfg = dict(engine.DEFAULT_CONFIG)
    cfg.update({"styles": ["Tells"], "style_paths": [], "disabled_rules": []})
    return cfg


class ShippedRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = engine.load_rules(shipped_config())
        cls.ids = {r.id for r in cls.rules}

    def test_every_rule_file_loads_and_is_documented(self):
        files = {p.stem for p in TELLS.glob("*.toml")}
        self.assertEqual(files, self.ids)
        for r in self.rules:
            with self.subTest(rule=r.id):
                self.assertTrue(r.why.strip(), "why is required")
                self.assertTrue(r.message.strip())
                self.assertTrue(r.summary.strip())
                self.assertIn(r.level, engine.LEVELS)

    def test_ai_prose_fixture_trips_every_rule(self):
        text = (FIXTURES / "ai-prose.md").read_text(encoding="utf-8")
        findings = engine.check_text(text, config=shipped_config(), rules=self.rules)
        fired = {f.rule for f in findings}
        self.assertEqual(
            self.ids - fired, set(), f"rules that never fired: {sorted(self.ids - fired)}"
        )

    def test_clean_prose_fixture_is_clean(self):
        text = (FIXTURES / "clean-prose.md").read_text(encoding="utf-8")
        findings = engine.check_text(text, config=shipped_config(), rules=self.rules)
        self.assertEqual(findings, [], engine.format_text(findings, "clean-prose.md"))

    def test_error_rules_report_exit_worthy_findings(self):
        text = "To be clear, this fails."
        findings = engine.check_text(text, config=shipped_config(), rules=self.rules)
        self.assertTrue(engine.has_level(findings, "error"))


class KnownFalsePositivesTest(unittest.TestCase):
    """Sentences that look like a tell and are not. Each one is a calibration scar."""

    @classmethod
    def setUpClass(cls):
        cls.rules = engine.load_rules(shipped_config())

    def rules_hit(self, text: str) -> set[str]:
        return {f.rule for f in engine.check_text(text, config=shipped_config(), rules=self.rules)}

    def test_candour_shape_needs_the_adjective(self):
        self.assertNotIn(
            "CandourShape",
            self.rules_hit("I want to be on the rota next week because the release is due."),
        )
        self.assertNotIn("CandourShape", self.rules_hit("That was honest work, and it shows."))

    def test_factual_contrasts_are_not_negation_corrections(self):
        self.assertNotIn(
            "NegationCorrection", self.rules_hit("The rules are TOML rather than YAML.")
        )
        self.assertNotIn(
            "NegationCorrection",
            self.rules_hit("The flag is not set by default, so enable it first."),
        )

    def test_summary_words_mid_sentence_are_fine(self):
        self.assertNotIn(
            "SummaryCloser",
            self.rules_hit("The overall latency dropped, and the summary line shows it."),
        )

    def test_let_me_mid_sentence_is_fine(self):
        self.assertNotIn("ProcessNarration", self.rules_hit("The flag will let me skip the check."))

    def test_structural_punctuation_in_lists_and_tables(self):
        # A label dash in a bullet or a cell is layout; a semicolon in a bullet
        # is not (decision 2026-09-10), while a table cell keeps its exemption.
        text = "- Owner — platform\n- Next — the rota\n\n| a — b | c; d |\n|---|---|\n| e | f |\n"
        hits = self.rules_hit(text)
        self.assertNotIn("Semicolon", hits)
        self.assertNotIn("EmDashDensity", hits)
        self.assertNotIn("EmDashList", hits)
        self.assertIn("Semicolon", self.rules_hit("- T-1 done; T-2 filed"))

    def test_punctuation_rules_are_error_level(self):
        levels = {r.id: r.level for r in self.rules}
        self.assertEqual(levels["Semicolon"], "error")
        self.assertEqual(levels["EmDashDensity"], "error")

    def test_a_single_dash_in_a_short_paragraph_is_fine(self):
        self.assertNotIn(
            "EmDashDensity",
            self.rules_hit("The cache was removed in the last release — nobody noticed."),
        )

    def test_allowed_doublings(self):
        self.assertNotIn(
            "Repetition", self.rules_hit("I know that that is true, and he had had enough.")
        )


if __name__ == "__main__":
    unittest.main()
