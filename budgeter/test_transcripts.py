#!/usr/bin/env python3
"""Tests for budgeter/lib/transcripts.py — the machine-wide transcript reader.

Builds a throwaway ``~/.claude/projects`` layout: an interactive session with
a subagent sidechain and a duplicated assistant turn, a headless runner
worktree session, a stale file that must be skipped unopened, and a session
on a model with no weight.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from budgeter.lib import transcripts

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)
ALPHA_CWD = "D:\\Professional\\alpha"
RUNNER_CWD = "D:\\Professional\\alpha\\.runner-worktrees\\runner_x-1234"


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def usage(i=0, cw=0, cr=0, o=0) -> dict:
    return {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }


def user_rec(ts, text, cwd, entrypoint="cli", **extra) -> dict:
    rec = {
        "type": "user",
        "timestamp": iso(ts),
        "cwd": cwd,
        "entrypoint": entrypoint,
        "userType": "external",
        "message": {"role": "user", "content": text},
    }
    rec.update(extra)
    return rec


def asst_rec(ts, msg_id, model, use, cwd, sidechain=False) -> dict:
    return {
        "type": "assistant",
        "timestamp": iso(ts),
        "cwd": cwd,
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "usage": use,
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def make_projects(root: Path, now: datetime = NOW) -> dict:
    """Populate *root* like ~/.claude/projects and return the paths written."""
    paths = {}
    alpha = root / "D--Professional-alpha"
    paths["s1"] = write_jsonl(
        alpha / "s1.jsonl",
        [
            user_rec(now - timedelta(hours=3), "<local-command-caveat>Caveat: local", ALPHA_CWD),
            user_rec(now - timedelta(hours=3), "fix the thing\nplease", ALPHA_CWD),
            # Same API turn written as two lines (one per content block).
            asst_rec(
                now - timedelta(hours=3), "m1", "claude-opus-5", usage(10, 20, 100, 5), ALPHA_CWD
            ),
            asst_rec(
                now - timedelta(hours=3), "m1", "claude-opus-5", usage(10, 20, 100, 5), ALPHA_CWD
            ),
            asst_rec(
                now - timedelta(hours=2), "m2", "claude-sonnet-5", usage(1, 0, 50, 2), ALPHA_CWD
            ),
            # Older than the window: must be excluded by timestamp.
            asst_rec(
                now - timedelta(days=10), "m0", "claude-opus-5", usage(999, 0, 0, 999), ALPHA_CWD
            ),
        ],
    )
    paths["s1_sub"] = write_jsonl(
        alpha / "s1" / "subagents" / "agent-1.jsonl",
        [
            asst_rec(
                now - timedelta(hours=1),
                "m3",
                "claude-haiku-4-5-20251001",
                usage(3, 0, 0, 4),
                ALPHA_CWD,
                sidechain=True,
            )
        ],
    )
    runner = root / "D--Professional-alpha--runner-worktrees-runner-x-1234"
    paths["s2"] = write_jsonl(
        runner / "s2.jsonl",
        [
            user_rec(
                now - timedelta(hours=5),
                "- You are implementing step 1 of a plan.",
                RUNNER_CWD,
                "sdk-cli",
            ),
            asst_rec(
                now - timedelta(hours=5), "m4", "claude-opus-5", usage(2, 0, 200, 1), RUNNER_CWD
            ),
        ],
    )
    beta = root / "D--Professional-beta"
    paths["s3"] = write_jsonl(
        beta / "s3.jsonl",
        [
            asst_rec(
                now - timedelta(hours=1),
                "m5",
                "claude-opus-5",
                usage(5, 0, 0, 5),
                "D:\\Professional\\beta",
            )
        ],
    )
    stale = (SINCE - timedelta(days=1)).timestamp()
    os.utime(paths["s3"], (stale, stale))
    gamma = root / "D--Professional-gamma"
    paths["s4"] = write_jsonl(
        gamma / "s4.jsonl",
        [
            user_rec(now - timedelta(hours=1), "hello", "D:\\Professional\\gamma"),
            asst_rec(
                now - timedelta(hours=1),
                "m6",
                "claude-mystery-9",
                usage(7, 0, 0, 7),
                "D:\\Professional\\gamma",
            ),
        ],
    )
    return paths


class TestIterSessions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_projects(self.root)
        self.sessions = {s.session_id: s for s in transcripts.iter_sessions(SINCE, None, self.root)}

    def tearDown(self):
        self._tmp.cleanup()

    def test_dedupes_turns_and_folds_subagents_into_the_parent(self):
        s1 = self.sessions["s1"]
        self.assertEqual(
            [c.model for c in s1.calls],
            ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
        )
        self.assertTrue(s1.calls[-1].sidechain)
        self.assertEqual(
            s1.calls[0].tokens, {"input": 10, "cache_write": 20, "cache_read": 100, "output": 5}
        )

    def test_window_excludes_old_calls_by_timestamp(self):
        self.assertNotIn("m0", [c.model for c in self.sessions["s1"].calls])
        self.assertEqual(sum(c.tokens["output"] for c in self.sessions["s1"].calls), 11)

    def test_first_prompt_skips_plumbing_and_flattens_whitespace(self):
        self.assertEqual(self.sessions["s1"].first_prompt, "fix the thing please")

    def test_interactive_session_label_and_kind(self):
        s1 = self.sessions["s1"]
        self.assertFalse(s1.headless)
        self.assertEqual(s1.kind, "interactive")
        self.assertEqual(s1.label, "alpha")

    def test_runner_worktree_session_is_headless_and_collapses_to_repo(self):
        s2 = self.sessions["s2"]
        self.assertTrue(s2.headless)
        self.assertEqual(s2.label, "alpha (runner worktrees)")

    def test_stale_files_are_skipped(self):
        self.assertNotIn("s3", self.sessions)

    def test_until_bounds_the_window(self):
        until = NOW - timedelta(hours=4)
        sessions = {s.session_id: s for s in transcripts.iter_sessions(SINCE, until, self.root)}
        self.assertIn("s2", sessions)
        self.assertNotIn("s1", sessions)

    def test_missing_root_yields_nothing(self):
        self.assertEqual(list(transcripts.iter_sessions(SINCE, None, self.root / "nope")), [])


class TestWeights(unittest.TestCase):
    def test_defaults_when_config_has_no_table(self):
        w = transcripts.Weights.from_config({})
        self.assertEqual(w.entry_for("claude-opus-5"), {"input": 5.0, "output": 25.0})
        self.assertEqual(w.cache_read_factor, transcripts.DEFAULT_CACHE_READ_FACTOR)

    def test_longest_prefix_match_for_dated_ids(self):
        w = transcripts.Weights.from_config({})
        self.assertEqual(w.entry_for("claude-haiku-4-5-20251001"), {"input": 1.0, "output": 5.0})
        self.assertEqual(w.entry_for("claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(w.entry_for("claude-fable-5")["input"], 10.0)
        self.assertIsNone(w.entry_for("claude-mystery-9"))

    def test_load_arithmetic(self):
        w = transcripts.Weights.from_config({})
        tokens = {"input": 10, "cache_write": 20, "cache_read": 100, "output": 5}
        # 10*5 + 20*5*1.25 + 100*0.5 + 5*25 = 350 per 1e6
        self.assertAlmostEqual(w.load("claude-opus-5", tokens), 350e-6)
        # Fable 5.1 overrides the cache-read rate with an absolute value.
        self.assertAlmostEqual(
            w.load("claude-fable-5-1", tokens),
            (10 * 10 + 20 * 10 * 1.25 + 100 * 0.25 + 5 * 50) / 1e6,
        )
        self.assertEqual(w.load("claude-mystery-9", tokens), 0.0)

    def test_config_table_and_factors_override_defaults(self):
        w = transcripts.Weights.from_config(
            {
                "model_weights": {"x": {"input": 1, "output": 1}},
                "cache_read_factor": 0.5,
                "cache_write_factor": 2,
            }
        )
        self.assertAlmostEqual(
            w.load("x", {"input": 0, "cache_write": 1, "cache_read": 1, "output": 0}), 2.5e-6
        )


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_projects(self.root)
        self.sessions = list(transcripts.iter_sessions(SINCE, None, self.root))
        self.weights = transcripts.Weights.from_config({})

    def tearDown(self):
        self._tmp.cleanup()

    def test_by_project_groups_and_reports_unweighted(self):
        buckets = transcripts.aggregate(self.sessions, self.weights, by="project")
        self.assertEqual(buckets.pop("__unweighted__"), ["claude-mystery-9"])
        self.assertEqual(set(buckets), {"alpha", "alpha (runner worktrees)", "gamma"})
        alpha = buckets["alpha"]
        self.assertEqual(alpha.calls, 3)
        self.assertEqual(alpha.tokens["cache_read"], 150)
        self.assertGreater(alpha.kinds["interactive"], 0)
        self.assertEqual(alpha.kinds["headless"], 0)
        self.assertGreater(buckets["alpha (runner worktrees)"].kinds["headless"], 0)
        self.assertEqual(buckets["gamma"].load, 0.0)

    def test_by_session_carries_label_and_prompt(self):
        buckets = transcripts.aggregate(self.sessions, self.weights, by="session")
        buckets.pop("__unweighted__")
        s1 = buckets["D--Professional-alpha/s1"]
        self.assertEqual(s1.label, "alpha")
        self.assertEqual(s1.first_prompt, "fix the thing please")
        self.assertEqual(s1.as_dict()["sessions"], 1)

    def test_by_model_day_and_kind(self):
        by_model = transcripts.aggregate(self.sessions, self.weights, by="model")
        self.assertIn("claude-opus-5", by_model)
        self.assertEqual(by_model["claude-opus-5"].calls, 2)
        by_kind = transcripts.aggregate(self.sessions, self.weights, by="kind")
        self.assertEqual(set(by_kind) - {"__unweighted__"}, {"interactive", "headless"})
        by_day = transcripts.aggregate(self.sessions, self.weights, by="day")
        self.assertTrue(all(len(k) == 10 for k in by_day if k != "__unweighted__"))

    def test_unknown_grouping_rejected(self):
        with self.assertRaises(ValueError):
            transcripts.aggregate(self.sessions, self.weights, by="colour")


class TestLabelsAndParsing(unittest.TestCase):
    def test_project_label_variants(self):
        self.assertEqual(transcripts.project_label("D:\\Professional\\alpha", "x"), "alpha")
        self.assertEqual(
            transcripts.project_label("D:/Professional/alpha/.claude/worktrees/agent-abc", "x"),
            "alpha (agent worktrees)",
        )
        self.assertEqual(
            transcripts.project_label(
                "C:\\Users\\me\\AppData\\Local\\Temp\\claude\\D--Professional-alpha"
                "\\0123abcd-1111-2222-3333-444444444444\\scratchpad\\wt",
                "x",
            ),
            "alpha (scratchpad)",
        )
        self.assertEqual(transcripts.project_label("", "D--Professional-alpha"), "alpha")

    def test_subdirectory_cwd_resolves_to_the_containing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "finances"
            (repo / ".git").mkdir(parents=True)
            deep = repo / "relay" / "data" / "staging"
            deep.mkdir(parents=True)
            self.assertEqual(transcripts.project_label(str(deep), "x"), "finances")
            self.assertEqual(transcripts.project_label(str(repo), "x"), "finances")
        # A path that does not exist falls back to its last segment.
        self.assertEqual(transcripts.project_label("D:\\nope\\deep\\dir", "x"), "dir")

    def test_synthetic_turns_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(
                root / "D--Professional-alpha" / "s9.jsonl",
                [
                    asst_rec(NOW, "z1", "<synthetic>", usage(0, 0, 0, 0), ALPHA_CWD),
                    asst_rec(NOW, "z2", "claude-opus-5", usage(1, 0, 0, 1), ALPHA_CWD),
                ],
            )
            sessions = list(transcripts.iter_sessions(SINCE, None, root))
            self.assertEqual([c.model for c in sessions[0].calls], ["claude-opus-5"])

    def test_parse_since_durations_and_dates(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(transcripts.parse_since("7d", now), now - timedelta(days=7))
        self.assertEqual(transcripts.parse_since("36h", now), now - timedelta(hours=36))
        self.assertEqual(transcripts.parse_since("90m", now), now - timedelta(minutes=90))
        self.assertEqual(
            transcripts.parse_since("2026-09-01", now), datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        with self.assertRaises(ValueError):
            transcripts.parse_since("yesterday", now)


if __name__ == "__main__":
    unittest.main()
