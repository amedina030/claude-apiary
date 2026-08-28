#!/usr/bin/env python3
"""Unit tests for runner/stage_lib.py — the shared LLM-stage plumbing.

These used to be five separate JSON salvagers, two copies of the retry loop
and six copies of the UUID guard, none of which had tests of their own except
through executor.parse_verify_output.
"""

import json
import subprocess
import unittest

from runner import stage_lib


class TestUuidGuard(unittest.TestCase):
    def test_accepts_plain_ids(self):
        for value in ("abc123", "e2e-detached-0001", "a.b.c"):
            self.assertTrue(stage_lib.is_uuid_safe(value), value)

    def test_rejects_traversal_and_junk(self):
        for value in (
            "",
            "   ",
            ".",
            "..",
            "a/b",
            "a\\b",
            "a\x00b",
            "../etc/passwd",
            None,
            17,
            ["x"],
        ):
            self.assertFalse(stage_lib.is_uuid_safe(value), repr(value))

    def test_check_returns_stripped_value(self):
        self.assertEqual(stage_lib.check_uuid_safe("  abc  "), "abc")

    def test_check_raises_with_label(self):
        with self.assertRaises(ValueError) as ctx:
            stage_lib.check_uuid_safe("a/b", "Plan uuid")
        self.assertIn("Plan uuid", str(ctx.exception))

    def test_check_raises_on_non_string(self):
        with self.assertRaises(ValueError):
            stage_lib.check_uuid_safe(17)


class TestExtractText(unittest.TestCase):
    def test_unwraps_envelope(self):
        raw = json.dumps({"result": "hello", "usage": {}})
        self.assertEqual(stage_lib.extract_text(raw), "hello")

    def test_passes_through_non_envelope(self):
        self.assertEqual(stage_lib.extract_text("plain text"), "plain text")

    def test_passes_through_envelope_without_result(self):
        raw = json.dumps({"other": 1})
        self.assertEqual(stage_lib.extract_text(raw), raw)


class TestExtractJson(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(stage_lib.extract_json('{"a": 1}'), {"a": 1})

    def test_envelope_inner_json(self):
        raw = json.dumps({"result": '{"steps": [1]}'})
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",)), {"steps": [1]})

    def test_envelope_is_already_the_artifact(self):
        raw = json.dumps({"steps": [1], "usage": {}})
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",))["steps"], [1])

    def test_fenced_json(self):
        raw = 'prose\n```json\n{"steps": []}\n```\nmore prose'
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",)), {"steps": []})

    def test_unclosed_fence(self):
        raw = '```json\n{"steps": []}'
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",)), {"steps": []})

    def test_json_in_prose(self):
        raw = 'Here you go: {"steps": [2]} — hope that helps'
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",)), {"steps": [2]})

    def test_prefers_the_object_with_the_required_keys(self):
        raw = '{"note": "ignore me"} then {"steps": [3]}'
        self.assertEqual(stage_lib.extract_json(raw, require_keys=("steps",)), {"steps": [3]})

    def test_falls_back_to_first_object_when_none_match(self):
        raw = 'nope {"note": "only this"}'
        self.assertEqual(
            stage_lib.extract_json(raw, require_keys=("steps",)),
            {"note": "only this"},
        )

    def test_unescaped_newlines_inside_strings(self):
        raw = '{"steps": [{"code_spec": "line one\nline two"}]}'
        parsed = stage_lib.extract_json(raw, require_keys=("steps",))
        self.assertEqual(parsed["steps"][0]["code_spec"], "line one\nline two")

    def test_top_level_array(self):
        self.assertEqual(stage_lib.extract_json("[1, 2]"), [1, 2])

    def test_array_rejected_when_lists_disallowed(self):
        with self.assertRaises(json.JSONDecodeError):
            stage_lib.extract_json("[1, 2]", allow_list=False)

    def test_nothing_parseable_raises(self):
        for raw in ("", "just prose", "not json {but has braces"):
            with self.assertRaises(json.JSONDecodeError):
                stage_lib.extract_json(raw)

    def test_extract_json_str_canonicalises_and_drops_trailing(self):
        raw = '[{"a": 1}] and then some prose'
        self.assertEqual(stage_lib.extract_json_str(raw), '[{"a": 1}]')

    def test_extract_json_str_returns_input_when_unparseable(self):
        self.assertEqual(stage_lib.extract_json_str("  nope  "), "nope")


class TestSanitizeJsonNewlines(unittest.TestCase):
    def test_escapes_inside_strings_only(self):
        text = '{"a": "x\ny"}\n'
        self.assertEqual(stage_lib.sanitize_json_newlines(text), '{"a": "x\\ny"}\n')

    def test_preserves_escaped_quotes(self):
        text = '{"a": "he said \\"hi\\"\nnext"}'
        out = stage_lib.sanitize_json_newlines(text)
        self.assertEqual(json.loads(out)["a"], 'he said "hi"\nnext')


class TestRetryUntilValid(unittest.TestCase):
    """The loop auto_refine and auto_plan each carried a copy of."""

    def _loop(self, *, calls, validations, max_attempts=3):
        prompts = []
        persisted = []
        call_iter = iter(calls)
        validation_iter = iter(validations)

        ok, artifact, errors = stage_lib.retry_until_valid(
            build_prompt=lambda prev: prompts.append(prev) or "prompt",
            call_model=lambda prompt: next(call_iter),
            parse=json.loads,
            assemble=lambda parsed: dict(parsed, stamped=True),
            persist=persisted.append,
            validate=lambda: next(validation_iter),
            max_attempts=max_attempts,
        )
        return ok, artifact, errors, prompts, persisted

    def test_first_attempt_valid(self):
        ok, artifact, errors, prompts, persisted = self._loop(
            calls=[(0, '{"a": 1}', "")],
            validations=[[]],
        )
        self.assertTrue(ok)
        self.assertEqual(artifact, {"a": 1, "stamped": True})
        self.assertEqual(errors, [])
        self.assertEqual(prompts, [None])
        self.assertEqual(len(persisted), 1)

    def test_validator_errors_are_fed_back_into_the_next_prompt(self):
        ok, artifact, _errors, prompts, _p = self._loop(
            calls=[(0, '{"a": 1}', ""), (0, '{"a": 2}', "")],
            validations=[["bad thing"], []],
        )
        self.assertTrue(ok)
        self.assertEqual(prompts, [None, ["bad thing"]])
        self.assertEqual(artifact["a"], 2)

    def test_exhaustion_returns_the_attempt_with_fewest_errors(self):
        ok, artifact, errors, _prompts, persisted = self._loop(
            calls=[(0, '{"a": 1}', ""), (0, '{"a": 2}', ""), (0, '{"a": 3}', "")],
            validations=[["x", "y"], ["z"], ["p", "q", "r"]],
        )
        self.assertFalse(ok)
        self.assertEqual(artifact["a"], 2)
        self.assertEqual(errors, ["z"])
        self.assertEqual(len(persisted), 3)

    def test_non_zero_exit_costs_an_attempt_and_reports_stderr(self):
        ok, _artifact, _errors, prompts, _p = self._loop(
            calls=[(1, "", "boom"), (0, '{"a": 1}', "")],
            validations=[[]],
        )
        self.assertTrue(ok)
        self.assertEqual(prompts[1], ["Claude Code failed: boom"])

    def test_timeout_costs_an_attempt(self):
        state = {"n": 0}
        prompts = []

        def _call(prompt):
            state["n"] += 1
            if state["n"] == 1:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
            return (0, '{"a": 1}', "")

        ok, artifact, errors = stage_lib.retry_until_valid(
            build_prompt=lambda prev: prompts.append(prev) or "p",
            call_model=_call,
            parse=json.loads,
            assemble=lambda p: p,
            persist=lambda a: None,
            validate=lambda: [],
            max_attempts=2,
        )
        self.assertTrue(ok)
        self.assertEqual(artifact, {"a": 1})
        self.assertEqual(errors, [])
        self.assertEqual(prompts[1], ["Claude Code subprocess timed out"])

    def test_unparseable_output_costs_an_attempt(self):
        ok, _artifact, _errors, prompts, _p = self._loop(
            calls=[(0, "not json", ""), (0, '{"a": 1}', "")],
            validations=[[]],
        )
        self.assertTrue(ok)
        self.assertIn("not valid JSON", prompts[1][0])

    def test_missing_cli_raises_instead_of_burning_attempts(self):
        def _call(prompt):
            raise FileNotFoundError("claude")

        with self.assertRaises(stage_lib.ClaudeMissingError):
            stage_lib.retry_until_valid(
                build_prompt=lambda prev: "p",
                call_model=_call,
                parse=json.loads,
                assemble=lambda p: p,
                persist=lambda a: None,
                validate=lambda: [],
            )


class TestIterUnique(unittest.TestCase):
    def test_preserves_order_and_drops_blanks(self):
        self.assertEqual(
            stage_lib.iter_unique(["a", "b", "a", "", None, "c"]),
            ["a", "b", "c"],
        )


if __name__ == "__main__":
    unittest.main()
