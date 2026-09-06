"""Tests for compass.store — path resolution for the rule-table pipeline."""

import os
import tempfile
import unittest
from pathlib import Path

from compass import store


class PathsTest(unittest.TestCase):
    def test_pipeline_paths_use_the_8char_short_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            full = "deadbeef-1234-5678-9abc-def012345678"
            self.assertEqual(store.short_session_id(full), "deadbeef")
            self.assertEqual(store.turns_path(full, tmp_path).name, "deadbeef.jsonl")
            self.assertEqual(store.cursor_path(full, tmp_path).name, "deadbeef.cursor.json")
            self.assertEqual(store.events_path(full, tmp_path).name, "deadbeef.json")
            self.assertEqual(
                store.heuristics_path(full, tmp_path).name, "deadbeef.heuristics.jsonl"
            )
            self.assertEqual(
                store.heuristics_path(full, tmp_path).parent,
                store.events_path(full, tmp_path).parent,
            )
            self.assertEqual(store.rules_path(tmp_path).name, "rules.md")
            self.assertEqual(store.manual_rules_path(tmp_path).name, "rules_manual.json")

    def test_seed_rules_ship_with_the_module(self):
        seed = store.load_seed_rules()
        self.assertEqual(
            [s["id"] for s in seed["sections"]], ["judgment", "output", "anticipation"]
        )
        ids = [r["id"] for r in seed["rules"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(seed["self_check"]["items"])

    def test_compass_dir_honors_target_state_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp).resolve() / "claude-apiary-1"
            state_dir.mkdir()
            prev = os.environ.get(store.TARGET_STATE_DIR_ENV)
            os.environ[store.TARGET_STATE_DIR_ENV] = str(state_dir)
            try:
                self.assertEqual(store.compass_dir(), state_dir / store.COMPASS_SUBDIR)
            finally:
                if prev is None:
                    del os.environ[store.TARGET_STATE_DIR_ENV]
                else:
                    os.environ[store.TARGET_STATE_DIR_ENV] = prev

    def test_compass_dir_falls_back_to_repo_root_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            prev = os.environ.get(store.TARGET_STATE_DIR_ENV)
            if prev is not None:
                del os.environ[store.TARGET_STATE_DIR_ENV]
            try:
                # No env, no git repo — falls through to <start>/.apiary/compass.
                self.assertEqual(
                    store.compass_dir(start=tmp_path),
                    tmp_path / store.APIARY_STATE_DIRNAME / store.COMPASS_SUBDIR,
                )
            finally:
                if prev is not None:
                    os.environ[store.TARGET_STATE_DIR_ENV] = prev


if __name__ == "__main__":
    unittest.main()
