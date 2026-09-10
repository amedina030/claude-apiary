"""Tests for prose/engine.py: rule loading, each check type, scopes, config, rendering."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import engine


class RuleDir:
    """A temp style directory; ``rule(name, toml)`` writes one rule file."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "Test"
        self.path.mkdir()

    def rule(self, name: str, toml: str) -> Path:
        p = self.path / f"{name}.toml"
        p.write_text(toml.strip() + "\n", encoding="utf-8")
        return p

    def config(self, **overrides) -> dict:
        cfg = dict(engine.DEFAULT_CONFIG)
        cfg.update({"styles": [], "style_paths": [str(self.path)]})
        cfg.update(overrides)
        return cfg

    def cleanup(self):
        self._tmp.cleanup()


class LoadingTest(unittest.TestCase):
    def setUp(self):
        self.d = RuleDir()
        self.addCleanup(self.d.cleanup)

    def test_existence_rule_loads_with_defaults(self):
        self.d.rule("Foo", 'extends = "existence"\nmessage = "no %s"\ntokens = ["bad phrase"]')
        rules = engine.load_rules(self.d.config())
        self.assertEqual([r.id for r in rules], ["Foo"])
        r = rules[0]
        self.assertEqual(
            (r.style, r.level, r.scopes, r.enabled), ("Test", "warning", ("text",), True)
        )
        self.assertEqual(r.summary, "no")

    def test_disabled_and_config_disabled_rules_are_skipped(self):
        self.d.rule("Off", 'extends = "existence"\nmessage = "x"\ntokens = ["a"]\nenabled = false')
        self.d.rule("On", 'extends = "existence"\nmessage = "x"\ntokens = ["a"]')
        self.d.rule("Cfg", 'extends = "existence"\nmessage = "x"\ntokens = ["a"]')
        rules = engine.load_rules(self.d.config(disabled_rules=["Cfg"]))
        self.assertEqual([r.id for r in rules], ["On"])

    def test_malformed_rules_name_the_file(self):
        cases = {
            "NoExtends": 'message = "x"\ntokens = ["a"]',
            "BadExtends": 'extends = "magic"\nmessage = "x"',
            "BadLevel": 'extends = "existence"\nmessage = "x"\ntokens = ["a"]\nlevel = "fatal"',
            "BadScope": 'extends = "existence"\nmessage = "x"\ntokens = ["a"]\nscope = ["chapter"]',
            "NoTokens": 'extends = "existence"\nmessage = "x"',
            "BadRegex": 'extends = "existence"\nmessage = "x"\nraw = ["(unclosed"]',
            "NoSwap": 'extends = "substitution"\nmessage = "x"',
            "NoLimit": 'extends = "occurrence"\nmessage = "x"\ntoken = ";"',
            "NotToml": "this is = = not toml",
        }
        for name, body in cases.items():
            path = self.d.rule(name, body)
            with self.subTest(name=name):
                with self.assertRaises(engine.RuleError) as cm:
                    engine.parse_rule(path, "Test")
                self.assertIn(name, str(cm.exception))


class ExistenceTest(unittest.TestCase):
    def setUp(self):
        self.d = RuleDir()
        self.addCleanup(self.d.cleanup)

    def check(self, text, **cfg):
        return engine.check_text(text, config=self.d.config(**cfg))

    def test_tokens_are_word_bounded_and_case_insensitive_when_asked(self):
        self.d.rule(
            "Tok",
            'extends = "existence"\nmessage = "phrase %s"\ntokens = ["to be clear"]\nignorecase = true',
        )
        found = self.check("To be clear, this fails. The unclear one passes.")
        self.assertEqual([f.span for f in found], ["To be clear"])
        self.assertEqual(found[0].message, "phrase To be clear")
        self.assertEqual(self.check("she wants to be clearly seen"), [])

    def test_tokens_are_case_sensitive_by_default(self):
        self.d.rule("Tok", 'extends = "existence"\nmessage = "x"\ntokens = ["Delve"]')
        self.assertEqual(len(self.check("Delve in. delve out.")), 1)

    def test_raw_regex_supports_lookahead_and_start_anchor(self):
        self.d.rule(
            "Raw",
            r"""
extends = "existence"
message = "shape %s"
scope = ["sentence"]
raw = ['\A(?:To be|Being)\s+honest\b(?=\s*[,.])']
""",
        )
        found = self.check("To be honest, it broke. It was honest work. Being honest, yes.")
        self.assertEqual([f.span for f in found], ["To be honest", "Being honest"])
        self.assertEqual([f.line for f in found], [1, 1])

    def test_paragraph_scope_ignores_lists_and_code(self):
        self.d.rule(
            "P", 'extends = "existence"\nmessage = "x"\nscope = ["paragraph"]\ntokens = ["tell"]'
        )
        text = "A tell here.\n\n- a tell in a list\n\n```\ntell in code\n```\n"
        found = self.check(text)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].line, found[0].scope), (1, "paragraph"))

    def test_multiple_scopes_union_without_duplicates(self):
        self.d.rule(
            "Both",
            'extends = "existence"\nmessage = "x"\nscope = ["text", "paragraph"]\ntokens = ["tell"]',
        )
        found = self.check("A tell here.\n\n- list tell")
        self.assertEqual([(f.line, f.scope) for f in found], [(1, "paragraph"), (3, "list")])

    def test_line_numbers_follow_soft_breaks_inside_a_paragraph(self):
        self.d.rule("L", 'extends = "existence"\nmessage = "x"\ntokens = ["tell"]')
        found = self.check("# H\n\nfirst line\nsecond line with a tell\nthird")
        self.assertEqual(found[0].line, 4)

    def test_context_and_span_are_collapsed_and_bounded(self):
        self.d.rule("C", 'extends = "existence"\nmessage = "x"\ntokens = ["tell"]')
        found = self.check("word " * 30 + "a\ntell\n" + "word " * 30, context_chars=10)
        self.assertEqual(found[0].span, "tell")
        self.assertTrue(found[0].context.startswith("...") and found[0].context.endswith("..."))
        self.assertNotIn("\n", found[0].context)
        self.assertLess(len(found[0].context), 40)


class OtherChecksTest(unittest.TestCase):
    def setUp(self):
        self.d = RuleDir()
        self.addCleanup(self.d.cleanup)

    def check(self, text, **cfg):
        return engine.check_text(text, config=self.d.config(**cfg))

    def test_substitution_reports_match_and_preferred(self):
        self.d.rule(
            "Swap",
            'extends = "substitution"\nmessage = "use %s -> %s"\nignorecase = true\n[swap]\nutilize = "use"\n"in order to" = "to"',
        )
        found = self.check("We utilize it in order to win. Utilized is not matched.")
        self.assertEqual(
            [f.message for f in found], ["use utilize -> use", "use in order to -> to"]
        )

    def test_occurrence_max_is_per_block(self):
        self.d.rule(
            "Semi",
            'extends = "occurrence"\nmessage = "%s semicolons, max %s"\nscope = ["paragraph"]\ntoken = ";"\nmax = 0',
        )
        found = self.check("One; two.\n\nClean paragraph.\n\n- item; ignored")
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].line, found[0].message), (1, "1 semicolons, max 0"))

    def test_occurrence_min(self):
        self.d.rule(
            "Need",
            'extends = "occurrence"\nmessage = "%s < %s"\nscope = ["paragraph"]\ntoken = "because"\nmin = 1',
        )
        found = self.check("No reason given.\n\nSecond because yes.")
        self.assertEqual([(f.line, f.message) for f in found], [(1, "0 < 1")])

    def test_density_budget_floor_and_rate(self):
        self.d.rule(
            "Dash",
            'extends = "density"\nmessage = "%s in %s words, allowed %s (one per %s)"\nscope = ["paragraph"]\ntoken = "—"\nrate = 1.0\nper_words = 100\nfloor = 1',
        )
        # 20 words, 1 dash: allowed = max(1, int(20*1/100)=0) = 1 -> clean.
        self.assertEqual(self.check("a — " + "w " * 18), [])
        # 20 words, 2 dashes -> over.
        found = self.check("a — b — " + "w " * 16)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].message, "2 in 20 words, allowed 1 (one per 10)")
        # 300 words, 3 dashes: allowed 3 -> clean; 4 -> over.
        self.assertEqual(self.check("— " * 3 + "w " * 297), [])
        self.assertEqual(len(self.check("— " * 4 + "w " * 296)), 1)

    def test_density_counts_across_blocks_in_scope_only(self):
        self.d.rule(
            "Dash",
            'extends = "density"\nmessage = "%s"\nscope = ["paragraph"]\ntoken = "—"\nfloor = 1',
        )
        text = "one — two.\n\nthree — four.\n\n- list — dash — dash — dash"
        found = self.check(text)
        self.assertEqual([f.message for f in found], ["2"])

    def test_repetition_with_exceptions(self):
        self.d.rule("Rep", 'extends = "repetition"\nmessage = "twice: %s"\nexceptions = ["that"]')
        found = self.check("It is is broken, and that that is fine. The The end.")
        self.assertEqual([f.span for f in found], ["is is", "The The"])


class ConfigAndRenderingTest(unittest.TestCase):
    def test_project_override_replaces_lists_and_repairs_bad_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".claude").mkdir()
            (repo / ".claude" / "prose.json").write_text(
                json.dumps(
                    {"include_globs": ["notes/*.md"], "min_level": "loud", "disabled_rules": ["X"]}
                ),
                encoding="utf-8",
            )
            cfg = engine.load_config(repo)
        self.assertEqual(cfg["include_globs"], ["notes/*.md"])
        self.assertEqual(cfg["disabled_rules"], ["X"])
        self.assertEqual(cfg["min_level"], engine.DEFAULT_CONFIG["min_level"])
        self.assertEqual(cfg["styles"], engine.DEFAULT_CONFIG["styles"])

    def test_path_included(self):
        cfg = dict(engine.DEFAULT_CONFIG)
        self.assertTrue(engine.path_included("docs/guide.md", cfg))
        self.assertTrue(engine.path_included("README.md", cfg))
        self.assertTrue(engine.path_included("D:/x/y/deep/note.md".replace("D:/", ""), cfg))
        self.assertFalse(engine.path_included("prose/engine.py", cfg))
        self.assertFalse(engine.path_included(".repos/foo-1/scribe/todos/1.md", cfg))
        self.assertFalse(engine.path_included("_tmp_scratch/x.md", cfg))
        self.assertFalse(engine.path_included("node_modules/pkg/README.md", cfg))

    def test_filter_has_level_and_counts(self):
        f = [
            engine.Finding("A", "error", 1, "", "", "", "p"),
            engine.Finding("B", "warning", 2, "", "", "", "p"),
            engine.Finding("C", "suggestion", 3, "", "", "", "p"),
        ]
        self.assertEqual([x.rule for x in engine.filter_level(f, "warning")], ["A", "B"])
        self.assertTrue(engine.has_level(f, "error"))
        self.assertFalse(engine.has_level(f[1:], "error"))
        self.assertEqual(engine.counts(f), {"suggestion": 1, "warning": 1, "error": 1})
        self.assertEqual(engine.counts_line(f), "3 finding(s): 1 error, 1 warning, 1 suggestion")
        self.assertEqual(engine.counts_line([]), "clean")

    def test_format_text_and_json(self):
        f = [
            engine.Finding(
                "A", "error", 4, "bad", "...a bad thing...", "no bad", "paragraph", "x.md"
            )
        ]
        text = engine.format_text(f)
        self.assertEqual(
            text.splitlines(),
            [
                "x.md: 1 finding(s): 1 error",
                "  - line 4 [error] A: no bad",
                "      ...a bad thing...",
            ],
        )
        self.assertEqual(engine.format_text([], "y.md"), "y.md: clean")
        data = json.loads(engine.format_json(f))
        self.assertEqual(data[0]["rule"], "A")
        self.assertEqual(data[0]["line"], 4)

    def test_format_message_pads_and_truncates_args(self):
        self.assertEqual(engine.format_message("a %s b %s", "x"), "a x b ")
        self.assertEqual(engine.format_message("plain", "x", "y"), "plain")
        self.assertEqual(engine.format_message("%s/%s", 1, 2, 3), "1/2")

    def test_summary_lines_follow_levels(self):
        d = RuleDir()
        self.addCleanup(d.cleanup)
        d.rule(
            "Err",
            'extends = "existence"\nmessage = "x"\ntokens = ["a"]\nlevel = "error"\nsummary = "No a."',
        )
        d.rule("Sug", 'extends = "existence"\nmessage = "y"\ntokens = ["b"]\nlevel = "suggestion"')
        rules = engine.load_rules(d.config())
        self.assertEqual(engine.summary_lines(rules), ["- Err [error]: No a."])
        self.assertEqual(len(engine.summary_lines(rules, "suggestion")), 2)


if __name__ == "__main__":
    unittest.main()
