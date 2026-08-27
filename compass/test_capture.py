#!/usr/bin/env python3
"""Tests for compass/capture.py — the /wrapup Step 4 capture CLI.

Hermetic: ``APIARY_TARGET_STATE_DIR`` points every run at a tempdir, so no test
can reach the live state store under ``<apiary>/.repos/``.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "capture.py")
PYTHON = sys.executable

SID = "abc12345"


def make_payload(**overrides) -> dict:
    payload = {
        "session_id": SID,
        "captured_at": "2026-08-26T12:00:00Z",
        "observations": [
            {
                "dimension": "communication_style",
                "observation": "Types terse lowercase prompts but expects full prose back.",
                "evidence": '"keep it short"',
                "volatility": "stable",
            }
        ],
    }
    payload.update(overrides)
    return payload


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args) -> subprocess.CompletedProcess:
        env = {**os.environ, "APIARY_TARGET_STATE_DIR": str(self.state)}
        return subprocess.run(
            [PYTHON, SCRIPT, *args],
            text=True,
            capture_output=True,
            env=env,
            encoding="utf-8",
            errors="replace",
        )

    def write(self, payload, name="obs.json") -> str:
        path = self.root / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload, indent=2), encoding="utf-8"
        )
        return str(path)

    @property
    def stored(self) -> Path:
        return self.state / "compass" / "observations" / f"{SID}.json"


class TestStore(CaptureTestCase):
    def test_valid_payload_is_written_to_the_state_dir(self):
        result = self.run_cli(
            "store", "--content-file", self.write(make_payload()), "--session-id", SID
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.stored.is_file())
        self.assertEqual(json.loads(self.stored.read_text(encoding="utf-8")), make_payload())
        payload = json.loads(result.stdout)
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["observations"], 1)

    def test_full_uuid_session_id_maps_to_the_eight_char_filename(self):
        full = "abc12345-1111-2222-3333-444455556666"
        result = self.run_cli(
            "store",
            "--content-file",
            self.write(make_payload(session_id=full)),
            "--session-id",
            full,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.stored.is_file())

    def test_session_id_is_taken_from_the_payload_when_not_passed(self):
        result = self.run_cli("store", "--content-file", self.write(make_payload()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.stored.is_file())

    def test_mismatched_session_id_is_rejected_and_nothing_is_written(self):
        result = self.run_cli(
            "store",
            "--content-file",
            self.write(make_payload(session_id="deadbeef")),
            "--session-id",
            SID,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match expected", result.stderr)
        self.assertFalse(self.stored.exists())

    def test_unknown_dimension_is_rejected(self):
        payload = make_payload()
        payload["observations"][0]["dimension"] = "vibes"
        result = self.run_cli("store", "--content-file", self.write(payload), "--session-id", SID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in configured dimensions", result.stderr)
        self.assertFalse(self.stored.exists())

    def test_missing_evidence_is_rejected(self):
        payload = make_payload()
        payload["observations"][0]["evidence"] = ""
        result = self.run_cli("store", "--content-file", self.write(payload), "--session-id", SID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("evidence missing or empty", result.stderr)

    def test_bad_volatility_is_rejected(self):
        payload = make_payload()
        payload["observations"][0]["volatility"] = "sometimes"
        result = self.run_cli("store", "--content-file", self.write(payload), "--session-id", SID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("volatility must be one of", result.stderr)

    def test_bad_captured_at_is_rejected(self):
        result = self.run_cli(
            "store",
            "--content-file",
            self.write(make_payload(captured_at="last tuesday")),
            "--session-id",
            SID,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not ISO-8601", result.stderr)

    def test_empty_observations_are_skipped_by_default(self):
        result = self.run_cli(
            "store",
            "--content-file",
            self.write(make_payload(observations=[])),
            "--session-id",
            SID,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no observations to store", result.stdout)
        self.assertFalse(self.stored.exists())

    def test_allow_empty_writes_the_file(self):
        result = self.run_cli(
            "store",
            "--content-file",
            self.write(make_payload(observations=[])),
            "--session-id",
            SID,
            "--allow-empty",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.stored.is_file())

    def test_markdown_fences_are_tolerated(self):
        wrapped = "```json\n" + json.dumps(make_payload()) + "\n```"
        result = self.run_cli("store", "--content-file", self.write(wrapped), "--session-id", SID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.stored.is_file())

    def test_invalid_json_is_a_clean_one_line_failure(self):
        result = self.run_cli(
            "store", "--content-file", self.write("{not json"), "--session-id", SID
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_file_is_a_clean_failure(self):
        result = self.run_cli(
            "store", "--content-file", str(self.root / "gone.json"), "--session-id", SID
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("observation file not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_file_is_a_clean_failure(self):
        result = self.run_cli("store", "--content-file", self.write(""), "--session-id", SID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is empty", result.stderr)

    def test_json_array_payload_is_rejected(self):
        result = self.run_cli("store", "--content-file", self.write("[1, 2]"), "--session-id", SID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected a JSON object", result.stderr)

    def test_dry_run_reports_the_target_without_writing(self):
        result = self.run_cli(
            "store", "--content-file", self.write(make_payload()), "--session-id", SID, "--dry-run"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["stored"])
        self.assertTrue(payload["dry_run"])
        self.assertIn(f"{SID}.json", payload["path"])
        self.assertFalse(self.stored.exists())

    def test_rewrite_replaces_the_previous_capture(self):
        self.run_cli("store", "--content-file", self.write(make_payload()), "--session-id", SID)
        second = make_payload()
        second["observations"].append(
            {
                "dimension": "mood_tone",
                "observation": "Impatient near the end of the session.",
                "evidence": '"just do it"',
                "volatility": "volatile",
            }
        )
        result = self.run_cli(
            "store", "--content-file", self.write(second, "obs2.json"), "--session-id", SID
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["observations"], 2)
        self.assertEqual(json.loads(result.stdout)["volatile"], 1)


class TestValidate(CaptureTestCase):
    def test_valid_payload_passes(self):
        result = self.run_cli(
            "validate", "--content-file", self.write(make_payload()), "--session-id", SID
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok — 1 observation(s)", result.stdout)

    def test_validate_never_writes(self):
        self.run_cli("validate", "--content-file", self.write(make_payload()), "--session-id", SID)
        self.assertFalse(self.stored.exists())

    def test_invalid_payload_lists_every_error(self):
        payload = make_payload()
        payload["observations"][0] = {
            "dimension": "vibes",
            "observation": "",
            "evidence": "",
            "volatility": "x",
        }
        result = self.run_cli(
            "validate", "--content-file", self.write(payload), "--session-id", SID
        )
        self.assertEqual(result.returncode, 1)
        self.assertGreaterEqual(result.stderr.count("  - "), 4)


class TestDimensionsAndTemplate(CaptureTestCase):
    def test_dimensions_lists_names_and_volatility(self):
        result = self.run_cli("dimensions")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("communication_style [stable]", result.stdout)
        self.assertIn("mood_tone [volatile]", result.stdout)

    def test_dimensions_json_is_the_raw_config(self):
        result = self.run_cli("dimensions", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dimensions", json.loads(result.stdout))

    def test_template_round_trips_through_validate(self):
        result = self.run_cli("template", "--session-id", SID)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["session_id"], SID)
        path = self.write(payload, "template.json")
        # The placeholder strings are non-empty, so the skeleton is structurally valid.
        self.assertEqual(
            self.run_cli("validate", "--content-file", path, "--session-id", SID).returncode, 0
        )


class TestCliShape(CaptureTestCase):
    def test_no_subcommand_is_a_usage_error(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)

    def test_help_lists_every_subcommand(self):
        result = self.run_cli("--help")
        for name in ("dimensions", "template", "validate", "store"):
            self.assertIn(name, result.stdout)


if __name__ == "__main__":
    unittest.main()
