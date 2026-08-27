#!/usr/bin/env python3
"""Every in-code config default must equal the value config.json ships.

Two of these had drifted apart (review subsystems/runner.md §"Config"):
``auto_harden.MAX_ROUNDS`` fell back to 3 while ``config.json`` said 1, and
``run.py``'s detached token cap fell back to 2,000,000 while ``config.json``
said 10,000,000 — so the same runner behaved differently depending on whether
it could read its own config, and the docs quoted whichever number their author
had read last.

The check is a source scan rather than a list of asserts on purpose: a new
``cfg("section", "key", <literal>)`` anywhere in ``runner/`` is covered the
moment it is written, with no test to remember to update.
"""

import ast
import json
import unittest
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parent
CONFIG_PATH = RUNNER_DIR / "config.json"

#: ``(section, key)`` pairs whose in-code default deliberately differs from the
#: shipped value, with the reason. Keep this list short and justified.
EXEMPT = {
    # The default is "no bans configured". Copying the shipped dictionary into
    # the source would duplicate the data config.json exists to hold, and the
    # call site already normalises a missing value with `or {}`.
    ("runner", "banned_tokens"): "config holds the data; the code default means 'none'",
}


def _config_call_defaults(path: Path):
    """Yield ``(lineno, section, key, default)`` for literal ``cfg(...)`` calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 3:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("cfg", "get"):
            continue
        section, key, default = node.args
        if not (isinstance(section, ast.Constant) and isinstance(key, ast.Constant)):
            continue
        if not isinstance(section.value, str) or not isinstance(key.value, str):
            continue
        try:
            value = ast.literal_eval(default)
        except ValueError:
            # A named constant or an expression — nothing to compare against.
            continue
        yield node.lineno, section.value, key.value, value


class TestConfigDefaultsMatchShippedConfig(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_every_literal_default_matches_config_json(self):
        mismatches = []
        checked = 0
        for path in sorted(RUNNER_DIR.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            for lineno, section, key, default in _config_call_defaults(path):
                if (section, key) in EXEMPT:
                    continue
                if section not in self.config or key not in self.config[section]:
                    continue
                checked += 1
                shipped = self.config[section][key]
                if default != shipped:
                    mismatches.append(
                        f"{path.name}:{lineno} cfg({section!r}, {key!r}, {default!r}) "
                        f"but config.json says {shipped!r}"
                    )
        self.assertGreater(checked, 10, "the scan found almost nothing — did cfg() get renamed?")
        self.assertEqual(
            mismatches,
            [],
            "in-code defaults have drifted from config.json:\n" + "\n".join(mismatches),
        )

    def test_the_two_that_had_drifted_are_pinned(self):
        """Named explicitly so a reader sees the numbers, not just the rule."""
        from runner import auto_harden

        self.assertEqual(self.config["harden"]["max_rounds"], 1)
        self.assertEqual(auto_harden.MAX_ROUNDS, 1)
        self.assertEqual(self.config["detached"]["token_cap"], 10000000)
        source = (RUNNER_DIR / "run.py").read_text(encoding="utf-8")
        self.assertIn('cfg("detached", "token_cap", 10000000)', source)

    def test_exempt_entries_still_exist_in_config(self):
        """An exemption for a key nobody ships any more is dead weight."""
        for section, key in EXEMPT:
            self.assertIn(section, self.config)
            self.assertIn(key, self.config[section])


if __name__ == "__main__":
    unittest.main()
