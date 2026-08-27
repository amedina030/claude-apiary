"""Tests for compass.evaluate — the label reducer, the fold logic, the
metric, and the A/B join.

Hermetic and deterministic: every test uses the stub synthesiser and a temp
state dir. ``NeverSpawnsClaudeTests`` asserts the property the whole suite
depends on — the default offline path never reaches ``run_claude``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import ab, evaluate, store  # noqa: E402
from compass import synthesize as synth  # noqa: E402

VOCAB = {
    "communication_style": {"terse": ["terse", "short"], "verbose": ["verbose"]},
    "autonomy": {"broad": ["broad"], "gated": ["gate"]},
    "mood_tone": {"positive": ["calm"], "negative": ["frustrated"], "neutral": ["steady"]},
}

DIMENSIONS = {
    "dimensions": [
        {"name": "communication_style", "volatile": False, "description": "d"},
        {"name": "autonomy", "volatile": False, "description": "d"},
        {"name": "mood_tone", "volatile": True, "description": "d"},
    ]
}


def observation(dimension: str, text: str) -> dict:
    return {
        "dimension": dimension,
        "observation": text,
        "evidence": "quote",
        "volatility": "stable",
    }


def session(sid: str, captured: str, *observations: dict) -> dict:
    return {"session_id": sid, "captured_at": captured, "observations": list(observations)}


class ReduceLabelTests(unittest.TestCase):
    def test_argmax_wins(self):
        self.assertEqual(
            evaluate.reduce_label("terse and short messages", "communication_style", VOCAB), "terse"
        )

    def test_no_cue_returns_none(self):
        self.assertIsNone(
            evaluate.reduce_label("nothing relevant here", "communication_style", VOCAB)
        )

    def test_tie_returns_none(self):
        self.assertIsNone(
            evaluate.reduce_label("terse but verbose", "communication_style", VOCAB),
            "a tie must abstain rather than guess",
        )

    def test_case_insensitive(self):
        self.assertEqual(evaluate.reduce_label("TERSE", "communication_style", VOCAB), "terse")

    def test_unknown_dimension_returns_none(self):
        self.assertIsNone(evaluate.reduce_label("terse", "not_a_dimension", VOCAB))

    def test_empty_text_returns_none(self):
        self.assertIsNone(evaluate.reduce_label("", "communication_style", VOCAB))

    def test_shipped_vocabulary_covers_every_dimension(self):
        shipped = evaluate.load_vocabulary()
        for name in store.dimension_names():
            self.assertIn(name, shipped, f"{name} has no label vocabulary")
            self.assertGreaterEqual(
                len(shipped[name]), 2, f"{name} needs at least two labels to be scorable"
            )


class SessionLabelsTests(unittest.TestCase):
    def test_one_label_per_dimension(self):
        s = session(
            "aaaa1111",
            "2026-01-01T00:00:00Z",
            observation("communication_style", "terse"),
            observation("autonomy", "broad latitude"),
        )
        self.assertEqual(
            evaluate.session_labels(s, VOCAB), {"communication_style": "terse", "autonomy": "broad"}
        )

    def test_two_observations_on_one_dimension_are_pooled(self):
        s = session(
            "aaaa1111",
            "2026-01-01T00:00:00Z",
            observation("communication_style", "terse"),
            observation("communication_style", "short and terse"),
        )
        self.assertEqual(evaluate.session_labels(s, VOCAB), {"communication_style": "terse"})

    def test_unlabelled_dimensions_are_dropped(self):
        s = session(
            "aaaa1111", "2026-01-01T00:00:00Z", observation("autonomy", "no cue in this text")
        )
        self.assertEqual(evaluate.session_labels(s, VOCAB), {})

    def test_evidence_is_never_read(self):
        s = session(
            "aaaa1111",
            "2026-01-01T00:00:00Z",
            {
                "dimension": "communication_style",
                "observation": "no cue",
                "evidence": "terse terse terse",
                "volatility": "stable",
            },
        )
        self.assertEqual(
            evaluate.session_labels(s, VOCAB), {}, "evidence is a raw session quote, not the claim"
        )


class ProfileSectionsTests(unittest.TestCase):
    def test_splits_on_h2(self):
        sections = evaluate.profile_sections(
            "# Profile\n\n## communication_style\nTerse.\n\n## autonomy\nBroad.\n"
        )
        self.assertEqual(sections["communication_style"], "Terse.")
        self.assertEqual(sections["autonomy"], "Broad.")

    def test_normalises_headings(self):
        sections = evaluate.profile_sections("## Communication Style\nTerse.\n")
        self.assertIn("communication_style", sections)

    def test_hyphenated_headings_normalise(self):
        sections = evaluate.profile_sections("## mood-tone\nCalm.\n")
        self.assertIn("mood_tone", sections)

    def test_empty_profile(self):
        self.assertEqual(evaluate.profile_sections(""), {})


class StubSynthesizeTests(unittest.TestCase):
    def test_emits_a_section_per_populated_dimension(self):
        training = [
            session("a", "2026-01-02T00:00:00Z", observation("communication_style", "terse"))
        ]
        out = evaluate.stub_synthesize(training, DIMENSIONS)
        self.assertIn("## communication_style", out)
        self.assertNotIn("## autonomy", out)

    def test_is_deterministic(self):
        training = [
            session("a", "2026-01-02T00:00:00Z", observation("autonomy", "broad")),
            session("b", "2026-01-01T00:00:00Z", observation("autonomy", "gate")),
        ]
        self.assertEqual(
            evaluate.stub_synthesize(training, DIMENSIONS),
            evaluate.stub_synthesize(training, DIMENSIONS),
        )

    def test_volatile_dimensions_only_see_the_recent_window(self):
        recent = [
            session(f"s{i}", f"2026-01-{20 - i:02d}T00:00:00Z", observation("mood_tone", "calm"))
            for i in range(evaluate.STUB_VOLATILE_WINDOW)
        ]
        old = [session("old", "2020-01-01T00:00:00Z", observation("mood_tone", "frustrated"))]
        out = evaluate.stub_synthesize(recent + old, DIMENSIONS)
        self.assertIn("calm", out)
        self.assertNotIn(
            "frustrated", out, "a volatile observation outside the window must not leak in"
        )

    def test_stable_dimensions_see_everything(self):
        training = [
            session(f"s{i}", f"2026-01-{20 - i:02d}T00:00:00Z", observation("autonomy", "broad"))
            for i in range(evaluate.STUB_VOLATILE_WINDOW + 3)
        ]
        training[-1]["observations"] = [observation("autonomy", "gate ancient")]
        self.assertIn("ancient", evaluate.stub_synthesize(training, DIMENSIONS))


class EvaluateFoldsTests(unittest.TestCase):
    def _corpus(self, n=6):
        return [
            session(
                f"{i:08x}",
                f"2026-01-{i + 1:02d}T00:00:00Z",
                observation("communication_style", "terse"),
                observation("autonomy", "broad"),
            )
            for i in range(n)
        ]

    def test_perfect_agreement_on_a_constant_corpus(self):
        result = evaluate.evaluate_folds(
            self._corpus(),
            VOCAB,
            DIMENSIONS,
            synthesizer=lambda t: evaluate.stub_synthesize(t, DIMENSIONS),
        )
        self.assertEqual(result["folds"], 6)
        self.assertEqual(result["pairs_evaluated"], 12)
        self.assertEqual(result["headline"], 1.0)
        self.assertEqual(result["majority"], 1.0)
        self.assertEqual(result["lift_over_majority"], 0.0)

    def test_held_out_session_is_never_in_the_training_set(self):
        corpus = self._corpus(4)
        seen = []

        def spy(training):
            seen.append({s["session_id"] for s in training})
            return evaluate.stub_synthesize(training, DIMENSIONS)

        evaluate.evaluate_folds(corpus, VOCAB, DIMENSIONS, synthesizer=spy)
        all_ids = {s["session_id"] for s in corpus}
        self.assertEqual(len(seen), 4)
        for i, training_ids in enumerate(seen):
            self.assertEqual(len(training_ids), 3)
            self.assertEqual(all_ids - training_ids, {corpus[i]["session_id"]})

    def test_a_wrong_profile_scores_zero(self):
        corpus = self._corpus(4)
        result = evaluate.evaluate_folds(
            corpus, VOCAB, DIMENSIONS, synthesizer=lambda t: "## communication_style\nverbose\n"
        )
        self.assertEqual(result["pairs_evaluated"], 4)
        self.assertEqual(result["headline"], 0.0)
        self.assertEqual(result["majority"], 1.0)
        self.assertEqual(result["lift_over_majority"], -1.0)

    def test_a_profile_with_no_sections_abstains(self):
        result = evaluate.evaluate_folds(
            self._corpus(3), VOCAB, DIMENSIONS, synthesizer=lambda t: "no headings"
        )
        self.assertEqual(result["pairs_evaluated"], 0)
        self.assertEqual(result["abstentions"], 6)
        self.assertIsNone(result["headline"])

    def test_majority_baseline_is_computed_on_the_training_split(self):
        # Five "broad", one "gated": the gated fold's training majority is
        # broad, so the majority baseline gets that one fold wrong.
        corpus = self._corpus(6)
        corpus[0]["observations"] = [observation("autonomy", "gate")]
        result = evaluate.evaluate_folds(
            corpus, VOCAB, DIMENSIONS, synthesizer=lambda t: evaluate.stub_synthesize(t, DIMENSIONS)
        )
        autonomy = result["per_dimension"]["autonomy"]
        self.assertEqual(autonomy["n"], 6)
        self.assertAlmostEqual(autonomy["majority_accuracy"], 5 / 6)

    def test_random_baseline_reflects_label_count(self):
        result = evaluate.evaluate_folds(
            self._corpus(3),
            VOCAB,
            DIMENSIONS,
            synthesizer=lambda t: evaluate.stub_synthesize(t, DIMENSIONS),
        )
        self.assertAlmostEqual(result["random"], 0.5, msg="both scored dimensions are binary")

    def test_per_label_precision(self):
        corpus = self._corpus(4)
        result = evaluate.evaluate_folds(
            corpus, VOCAB, DIMENSIONS, synthesizer=lambda t: "## autonomy\ngate\n"
        )
        precision = result["per_dimension"]["autonomy"]["precision"]
        self.assertEqual(precision["gated"]["predicted"], 4)
        self.assertEqual(precision["gated"]["correct"], 0)
        self.assertEqual(precision["gated"]["precision"], 0.0)

    def test_max_folds_bounds_the_run(self):
        calls = []
        evaluate.evaluate_folds(
            self._corpus(6),
            VOCAB,
            DIMENSIONS,
            max_folds=2,
            synthesizer=lambda t: calls.append(1) or evaluate.stub_synthesize(t, DIMENSIONS),
        )
        self.assertEqual(len(calls), 2)

    def test_too_few_sessions_reports_nothing(self):
        result = evaluate.evaluate_folds(
            self._corpus(1), VOCAB, DIMENSIONS, synthesizer=lambda t: ""
        )
        self.assertEqual(result["folds"], 0)
        self.assertIsNone(result["headline"])

    def test_coverage_counts_unlabelled_pairs(self):
        corpus = self._corpus(3)
        for s in corpus:
            s["observations"].append(observation("mood_tone", "no cue at all"))
        result = evaluate.evaluate_folds(
            corpus, VOCAB, DIMENSIONS, synthesizer=lambda t: evaluate.stub_synthesize(t, DIMENSIONS)
        )
        self.assertEqual(result["pairs_present"], 9)
        self.assertEqual(result["pairs_labelled"], 6)
        self.assertAlmostEqual(result["label_coverage"], 6 / 9)

    def test_progress_callback_is_invoked_per_fold(self):
        seen = []
        evaluate.evaluate_folds(
            self._corpus(3),
            VOCAB,
            DIMENSIONS,
            synthesizer=lambda t: evaluate.stub_synthesize(t, DIMENSIONS),
            progress=lambda i, n: seen.append((i, n)),
        )
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])


class EstimateCostTests(unittest.TestCase):
    def test_scales_with_folds(self):
        corpus = [
            session(f"{i:08x}", "2026-01-01T00:00:00Z", observation("autonomy", "broad"))
            for i in range(4)
        ]
        one = evaluate.estimate_cost(corpus, DIMENSIONS, 1, "opus")
        four = evaluate.estimate_cost(corpus, DIMENSIONS, 4, "opus")
        self.assertEqual(four["input_tokens"], one["input_tokens"] * 4)
        self.assertGreater(four["usd"], one["usd"])

    def test_unknown_model_has_no_price(self):
        corpus = [session("a", "2026-01-01T00:00:00Z", observation("autonomy", "broad"))]
        self.assertIsNone(evaluate.estimate_cost(corpus, DIMENSIONS, 1, "mystery")["usd"])

    def test_empty_corpus(self):
        self.assertEqual(evaluate.estimate_cost([], DIMENSIONS, 3, "opus")["folds"], 0)


class CorrectionHeuristicTests(unittest.TestCase):
    def test_detects_common_corrections(self):
        for text in [
            "no, that's not it",
            "revert that",
            "why did you do that",
            "actually, use B",
            "undo",
        ]:
            self.assertTrue(evaluate.is_correction(text), text)

    def test_ignores_plain_instructions(self):
        for text in ["add a test for the parser", "ship it", "looks good", ""]:
            self.assertFalse(evaluate.is_correction(text), text)


class SummariseArmsTests(unittest.TestCase):
    def _row(self, sid, turn, task, tokens, message="", timestamp="2026-08-01T00:00:00Z"):
        return {
            "session_id": sid,
            "turn_number": turn,
            "task_turn": task,
            "net_tokens_delta": tokens,
            "user_message": message,
            "timestamp": timestamp,
        }

    def test_groups_by_arm(self):
        rows = [
            self._row("on-session", 1, 1, 100),
            self._row("on-session", 2, 1, 100),
            self._row("off-session", 1, 1, 50),
        ]
        summary = evaluate.summarise_arms(
            rows, arm_lookup=lambda sid: "on" if sid.startswith("on") else "off"
        )
        self.assertEqual(summary["arms"]["on"]["sessions"], 1)
        self.assertEqual(summary["arms"]["off"]["sessions"], 1)
        self.assertEqual(summary["arms"]["on"]["tool_calls_per_task"], 2.0)
        self.assertEqual(summary["arms"]["off"]["tool_calls_per_task"], 1.0)

    def test_tasks_are_distinct_task_turns(self):
        rows = [self._row("s", 1, 1, 10), self._row("s", 2, 1, 10), self._row("s", 3, 2, 10)]
        summary = evaluate.summarise_arms(rows, arm_lookup=lambda sid: "on")
        self.assertEqual(summary["arms"]["on"]["tasks"], 2)
        self.assertEqual(summary["arms"]["on"]["net_tokens_per_task"], 15.0)

    def test_corrections_are_counted_once_per_user_turn(self):
        rows = [
            self._row("s", 1, 1, 10, "no, not like that"),
            self._row("s", 1, 1, 10, "no, not like that"),  # same turn, repeated
            self._row("s", 2, 1, 10, "add a test"),
        ]
        summary = evaluate.summarise_arms(rows, arm_lookup=lambda sid: "on")
        self.assertEqual(summary["arms"]["on"]["user_turns"], 2)
        self.assertEqual(summary["arms"]["on"]["corrections"], 1)

    def test_since_filters_rows(self):
        rows = [
            self._row("s", 1, 1, 10, timestamp="2026-07-01T00:00:00Z"),
            self._row("s", 2, 2, 10, timestamp="2026-08-15T00:00:00Z"),
        ]
        summary = evaluate.summarise_arms(rows, since="2026-08-01", arm_lookup=lambda sid: "on")
        self.assertEqual(summary["arms"]["on"]["rows"], 1)

    def test_empty_arm_reports_none_not_zero(self):
        summary = evaluate.summarise_arms([self._row("s", 1, 1, 10)], arm_lookup=lambda sid: "on")
        self.assertIsNone(
            summary["arms"]["off"]["tool_calls_per_task"],
            "an empty arm must not look like a measured zero",
        )

    def test_rows_without_a_session_id_are_skipped(self):
        summary = evaluate.summarise_arms(
            [{"timestamp": "2026-08-01", "task_turn": 1}], arm_lookup=lambda sid: "on"
        )
        self.assertEqual(summary["sessions_seen"], 0)


class ReadBudgeterLogTests(unittest.TestCase):
    def test_reads_a_temp_log_and_restores_the_global(self):
        from budgeter.lib import logger as budget_logger

        original = budget_logger.LOG_PATH
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage_log.jsonl"
            path.write_text('{"session_id": "s", "task_turn": 1}\n{"bad json\n', encoding="utf-8")
            rows = evaluate.read_budgeter_log(path)
        self.assertEqual(rows, [{"session_id": "s", "task_turn": 1}])
        self.assertEqual(budget_logger.LOG_PATH, original)


class StateDirSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.observations = self.state / "compass" / "observations"
        self.observations.mkdir(parents=True)
        self._previous = os.environ.get(store.TARGET_STATE_DIR_ENV)
        os.environ[store.TARGET_STATE_DIR_ENV] = str(self.state)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop(store.TARGET_STATE_DIR_ENV, None)
        else:
            os.environ[store.TARGET_STATE_DIR_ENV] = self._previous

    def _run(self, argv):
        """Run the CLI, capturing stdout and stderr separately."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = evaluate.main(argv)
        self._stderr = err.getvalue()
        return code, out.getvalue()

    def write_sessions(self, n=4):
        for i in range(n):
            payload = session(
                f"{i:08x}",
                f"2026-01-{i + 1:02d}T00:00:00Z",
                observation("communication_style", "terse single-line directives"),
                observation("autonomy", "grants broad latitude"),
            )
            (self.observations / f"{i:08x}.json").write_text(json.dumps(payload), encoding="utf-8")


class CacheTests(StateDirSandbox):
    def test_round_trip(self):
        evaluate.cache_result(
            {"mode": "stub", "headline": 0.8, "folds": 4, "lift_over_majority": 0.1}
        )
        cached = evaluate.load_cached_result()
        self.assertEqual(cached["headline"], 0.8)
        self.assertEqual(cached["mode"], "stub")
        self.assertIn("computed_at", cached)

    def test_missing_cache_reads_as_none(self):
        self.assertIsNone(evaluate.load_cached_result())

    def test_corrupt_cache_reads_as_none(self):
        path = evaluate.last_result_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops", encoding="utf-8")
        self.assertIsNone(evaluate.load_cached_result())


class CliTests(StateDirSandbox):
    def test_offline_json_end_to_end(self):
        self.write_sessions()
        code, out = self._run(["offline", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "stub")
        self.assertEqual(payload["folds"], 4)
        self.assertEqual(payload["headline"], 1.0)
        self.assertTrue(evaluate.last_result_path().is_file())

    def test_offline_human_output_names_the_baselines(self):
        self.write_sessions()
        code, out = self._run(["offline"])
        self.assertEqual(code, 0)
        for expected in (
            "HEADLINE",
            "majority baseline",
            "random baseline",
            "LIFT over majority",
            "coverage",
        ):
            self.assertIn(expected, out)

    def test_no_cache_flag_skips_the_write(self):
        self.write_sessions()
        self._run(["offline", "--json", "--no-cache"])
        self.assertFalse(evaluate.last_result_path().exists())

    def test_offline_needs_two_sessions(self):
        self.write_sessions(1)
        code, _ = self._run(["offline"])
        self.assertEqual(code, 1)

    def test_model_run_refuses_without_yes(self):
        self.write_sessions()
        code, out = self._run(["offline", "--model", "opus"])
        self.assertEqual(code, 2, "a spend must not happen without --yes")
        self.assertIn("estimated input", self._stderr)
        self.assertEqual(out, "", "the estimate must not pollute stdout")

    def test_labels_lists_every_dimension(self):
        code, out = self._run(["labels"])
        self.assertEqual(code, 0)
        for name in store.dimension_names():
            self.assertIn(name, out)

    def test_ab_reports_both_arms(self):
        log = self.root / "usage_log.jsonl"
        log.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "session_id": "aaaa0001",
                        "turn_number": 1,
                        "task_turn": 1,
                        "net_tokens_delta": 10,
                        "timestamp": "2026-08-01T00:00:00Z",
                    },
                    {
                        "session_id": "bbbb0002",
                        "turn_number": 1,
                        "task_turn": 1,
                        "net_tokens_delta": 20,
                        "timestamp": "2026-08-02T00:00:00Z",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        code, out = self._run(["ab", "--log", str(log), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["sessions_seen"], 2)
        self.assertEqual(set(payload["arms"]), set(ab.ARMS))

    def test_ab_says_when_the_experiment_is_off(self):
        log = self.root / "usage_log.jsonl"
        log.write_text(
            json.dumps(
                {
                    "session_id": "aaaa0001",
                    "turn_number": 1,
                    "task_turn": 1,
                    "net_tokens_delta": 10,
                    "timestamp": "2026-08-01T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        code, out = self._run(["ab", "--log", str(log)])
        self.assertEqual(code, 0)
        self.assertIn("DISABLED", out)

    def test_ab_with_an_empty_log(self):
        log = self.root / "empty.jsonl"
        log.write_text("", encoding="utf-8")
        code, _ = self._run(["ab", "--log", str(log)])
        self.assertEqual(code, 1)

    def test_state_dir_flag_redirects_the_store(self):
        other = self.root / "other-state"
        (other / "compass" / "observations").mkdir(parents=True)
        code, out = self._run(["--state-dir", str(other), "offline"])
        self.assertEqual(code, 1, "the redirected state dir has no observations")


class NeverSpawnsClaudeTests(StateDirSandbox):
    """The default offline path must never reach the claude subprocess."""

    def test_stub_run_does_not_call_run_claude(self):
        self.write_sessions()
        original = synth.run_claude

        def explode(*args, **kwargs):
            raise AssertionError("the stub synthesiser must never spawn claude")

        synth.run_claude = explode
        self.addCleanup(lambda: setattr(synth, "run_claude", original))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(evaluate.main(["offline", "--json"]), 0)

    def test_model_synthesize_unwraps_the_claude_envelope(self):
        seen = {}

        def fake_run(prompt, model=None):
            seen["model"] = model
            seen["prompt"] = prompt
            return 0, json.dumps({"result": "```md\n## autonomy\nbroad\n```"}), ""

        original = synth.run_claude
        synth.run_claude = fake_run
        self.addCleanup(lambda: setattr(synth, "run_claude", original))
        profile = evaluate.model_synthesize(
            [session("a", "2026-01-01T00:00:00Z", observation("autonomy", "broad"))],
            store.load_dimensions(),
            "sonnet",
        )
        self.assertEqual(seen["model"], "sonnet")
        self.assertIn("## autonomy", profile)
        self.assertNotIn("```", profile)
        self.assertIn("Active observations", seen["prompt"])
        self.assertIn(
            "(none — first synthesis)",
            seen["prompt"],
            "the live personality.md must not leak into a fold",
        )

    def test_model_path_runs_one_call_per_fold_when_confirmed(self):
        self.write_sessions(3)
        calls = []

        def fake_run(prompt, model=None):
            calls.append(model)
            return 0, json.dumps({"result": "## autonomy\nbroad\n"}), ""

        original = synth.run_claude
        synth.run_claude = fake_run
        self.addCleanup(lambda: setattr(synth, "run_claude", original))
        code, output = self._run(["offline", "--model", "opus", "--yes", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["opus", "opus", "opus"], "one synthesis call per fold, no more")
        payload = json.loads(output)
        self.assertEqual(payload["mode"], "model")
        self.assertEqual(payload["model"], "opus")

    def test_model_synthesize_survives_a_failing_subprocess(self):
        original = synth.run_claude
        synth.run_claude = lambda prompt, model=None: (1, "", "nope")
        self.addCleanup(lambda: setattr(synth, "run_claude", original))
        out = io.StringIO()
        with redirect_stdout(out):
            profile = evaluate.model_synthesize(
                [session("a", "2026-01-01T00:00:00Z", observation("autonomy", "broad"))],
                store.load_dimensions(),
                "opus",
            )
        self.assertEqual(profile, "")


if __name__ == "__main__":
    unittest.main()
