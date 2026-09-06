#!/usr/bin/env python3
"""Tests for runner/validate_post_conditions.py (T-2026-313): placeholder and
unresolvable-import ``file_contains`` texts are rejected at plan time."""

import tempfile
import unittest
from pathlib import Path

from runner import validate_post_conditions as vpc
from runner.test_validate_plan import _base_plan, _step
from runner.validate_plan import validate

TESTING_PY = """\
import os
from json import loads as jl

try:
    import fcntl
except ImportError:
    fcntl = None

CONST, OTHER = 1, 2
LIMIT: int = 3


def hermetic_env():
    return {}


class Fixture:
    pass
"""


class _RepoFixture(unittest.TestCase):
    """A throwaway target repo with one package, one module, one test file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        (self.repo / "core" / "sub").mkdir(parents=True)
        (self.repo / "core" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "core" / "testing.py").write_text(TESTING_PY, encoding="utf-8")
        (self.repo / "core" / "sub" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "runner").mkdir()
        (self.repo / "runner" / "test_x.py").write_text("x = 1\n", encoding="utf-8")

    def cond(self, text, file="runner/test_x.py", ctype="file_contains"):
        return {"type": ctype, "file": file, "text": text}

    def errors(self, conds, files=("runner/test_x.py",), extra_steps=()):
        step = _step(1, "modify", "code", files=list(files))
        step["post_conditions"] = [self.cond(c) if isinstance(c, str) else c for c in conds]
        return vpc.check_literals([step, *extra_steps], self.repo)


class LiteralCheckTests(_RepoFixture):
    # -- the night that motivated this ------------------------------------------------

    def test_the_2026_09_04_literal_is_rejected(self):
        errs = self.errors(["from core.testing import import_placeholder_never_used"])
        self.assertEqual(len(errs), 1)
        self.assertIn("looks like a placeholder", errs[0])
        self.assertIn("step[0].post_conditions[0]", errs[0])

    def test_same_shape_without_the_word_is_caught_by_the_import_check(self):
        errs = self.errors(["from core.testing import import_never_replaced_symbol"])
        self.assertEqual(len(errs), 1)
        self.assertIn("defines no such name", errs[0])
        self.assertIn("core/testing.py", errs[0])

    # -- placeholder tokens -----------------------------------------------------------

    def test_placeholder_words_rejected(self):
        for text in (
            "TODO: fill in",
            "lorem ipsum dolor",
            "x = PLACEHOLDER_VALUE",
            "FIXME later",
            "TBD",
        ):
            errs = self.errors([text])
            self.assertEqual(len(errs), 1, text)
            self.assertIn("placeholder", errs[0])

    def test_identifier_fragments_and_html_attributes_pass(self):
        for text in (
            "def todo_list(",
            'placeholder="Search notes"',
            "placeholder='x'",
            "xxx_yyy = 1",
        ):
            self.assertEqual(self.errors([text]), [], text)

    def test_file_lacks_is_exempt(self):
        self.assertEqual(self.errors([self.cond("TODO", ctype="file_lacks")]), [])
        self.assertEqual(self.errors([self.cond("never_used", ctype="file_lacks")]), [])

    # -- imports ------------------------------------------------------------------------

    def test_existing_symbols_pass(self):
        texts = [
            "from core.testing import hermetic_env",
            "from core.testing import hermetic_env as he, Fixture",
            "from core.testing import CONST, OTHER, LIMIT",
            "from core.testing import jl",  # re-export
            "from core.testing import os",  # plain import re-exported
            "from core.testing import fcntl",  # guarded import
            "from core.testing import *",
            "from core import testing",  # submodule of a package
            "from core import sub",  # subpackage
        ]
        self.assertEqual(self.errors(texts), [])

    def test_missing_symbol_rejected_unless_a_step_modifies_the_module(self):
        errs = self.errors(["from core.testing import nope"])
        self.assertEqual(len(errs), 1)
        self.assertIn("'nope'", errs[0])
        # The plan modifies the module: trust it.
        self.assertEqual(
            self.errors(
                ["from core.testing import nope"], files=("runner/test_x.py", "core/testing.py")
            ),
            [],
        )
        adder = _step(2, "modify", "add nope", files=["core/testing.py"])
        self.assertEqual(self.errors(["from core.testing import nope"], extra_steps=(adder,)), [])

    def test_missing_module_rejected_unless_a_step_creates_it(self):
        errs = self.errors(["from core.missing import thing"])
        self.assertEqual(len(errs), 1)
        self.assertIn("does not exist", errs[0])
        self.assertIn("core/missing.py", errs[0])
        creator = _step(2, "create", "new module", files=["core/missing.py"])
        self.assertEqual(
            self.errors(["from core.missing import thing"], extra_steps=(creator,)), []
        )

    def test_stdlib_and_third_party_are_not_checked(self):
        texts = [
            "from json import loads",
            "from requests import get",
            "import os",
            "import core.testing",
        ]
        self.assertEqual(self.errors(texts), [])

    def test_relative_imports_resolve_against_the_condition_file(self):
        ok = self.cond("from .testing import hermetic_env", file="core/test_y.py")
        self.assertEqual(self.errors([ok]), [])
        bad = self.cond("from .testing import nope", file="core/test_y.py")
        self.assertEqual(len(self.errors([bad])), 1)
        deep = self.cond("from ..testing import Fixture", file="core/sub/x.py")
        self.assertEqual(self.errors([deep]), [])
        too_far = self.cond("from ....testing import Fixture", file="core/sub/x.py")
        self.assertEqual(self.errors([too_far]), [])  # cannot anchor: skipped, not guessed
        no_file = {"type": "file_contains", "file": "", "text": "from .testing import nope"}
        self.assertEqual(self.errors([no_file]), [])

    def test_non_import_texts_are_untouched(self):
        texts = ["def foo(", "x = (", "class Fixture:", "    return {}", "HOOKS = ("]
        self.assertEqual(self.errors(texts), [])

    def test_multi_statement_text_checks_each_import(self):
        text = "import os\nfrom core.testing import hermetic_env\nfrom core.testing import nope\n"
        errs = self.errors([text])
        self.assertEqual(len(errs), 1)
        self.assertIn("'nope'", errs[0])

    # -- helpers ------------------------------------------------------------------------

    def test_planned_paths_normalises(self):
        steps = [_step(1, "create", "c", files=["./a/b.py", "c\\d.py", " e.py "]), "junk"]
        self.assertEqual(vpc.planned_paths(steps), {"a/b.py", "c/d.py", "e.py"})

    def test_top_level_names_none_on_unparseable(self):
        bad = self.repo / "core" / "broken.py"
        bad.write_text("def (:\n", encoding="utf-8")
        self.assertIsNone(vpc.top_level_names(bad))
        self.assertIsNone(vpc.top_level_names(self.repo / "core" / "absent.py"))


class ValidatePlanIntegrationTests(_RepoFixture):
    def test_validate_reports_the_literal_error(self):
        step = _step(1, "modify", "code", files=["runner/test_x.py"])
        step["post_conditions"] = [
            {
                "type": "file_contains",
                "file": "runner/test_x.py",
                "text": "from core.testing import import_placeholder_never_used",
            }
        ]
        errors = validate(_base_plan([step]), banned_tokens={}, repo_root=self.repo)
        self.assertTrue(any("looks like a placeholder" in e for e in errors), errors)

    def test_validate_accepts_a_real_anchor(self):
        step = _step(1, "modify", "code", files=["runner/test_x.py"])
        step["post_conditions"] = [
            {
                "type": "file_contains",
                "file": "runner/test_x.py",
                "text": "from core.testing import hermetic_env",
            }
        ]
        errors = validate(_base_plan([step]), banned_tokens={}, repo_root=self.repo)
        self.assertEqual([e for e in errors if "post_conditions" in e], [], errors)


if __name__ == "__main__":
    unittest.main()
