"""Tests for auto_harden.compute_verdict — the pure verdict-classification
function that distinguishes defender_failed from has_unresolved/all_resolved/
attacker_failed — and for the attacker/defender timeout-retry budget.
"""

import unittest
from unittest import mock

from runner import auto_harden
from runner.auto_harden import compute_verdict


class ComputeVerdictTests(unittest.TestCase):
    def test_clean_target_no_findings(self):
        rounds = [{"round": 1, "findings": [], "responses": [], "resolutions": {}}]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])

    def test_all_findings_fixed(self):
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
                "responses": [
                    {"finding_ref": "ATK-001", "action": "fixed"},
                    {"finding_ref": "ATK-002", "action": "refactored"},
                ],
                "resolutions": {"ATK-001": "fixed", "ATK-002": "refactored"},
            }
        ]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])

    def test_has_unresolved_with_deferred(self):
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
                "responses": [
                    {"finding_ref": "ATK-001", "action": "fixed"},
                    {"finding_ref": "ATK-002", "action": "deferred"},
                ],
                "resolutions": {"ATK-001": "fixed", "ATK-002": "deferred"},
            }
        ]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "has_unresolved")
        self.assertEqual(unresolved, ["ATK-002"])

    def test_defender_failed_none_response(self):
        # run_defender returned None — resolutions explicitly marked unresolved
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
                "responses": [],
                "resolutions": {"ATK-001": "unresolved", "ATK-002": "unresolved"},
            }
        ]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")
        self.assertEqual(sorted(unresolved), ["ATK-001", "ATK-002"])

    def test_defender_failed_empty_response_array(self):
        # run_defender returned [] — resolutions is empty (latent bug pre-fix).
        # compute_verdict must still flag this as defender_failed.
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}],
                "responses": [],
                "resolutions": {},
            }
        ]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")
        self.assertEqual(unresolved, [])

    def test_defender_failed_takes_priority_over_has_unresolved(self):
        # If any round had findings-without-responses, the whole run is flagged
        # defender_failed even if another round produced normal output. In the
        # runner (max_rounds=1) this path is unreachable, but the contract
        # should be clear for future multi-round use.
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}],
                "responses": [],
                "resolutions": {"ATK-001": "unresolved"},
            },
            {
                "round": 2,
                "findings": [{"id": "ATK-002"}],
                "responses": [{"finding_ref": "ATK-002", "action": "fixed"}],
                "resolutions": {"ATK-002": "fixed"},
            },
        ]
        verdict, _ = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")

    def test_empty_rounds_list(self):
        # Edge case: auto_harden aborted before any round completed
        verdict, unresolved = compute_verdict([])
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])

    def test_attacker_failed_with_no_rounds(self):
        # The common shape: attacker times out in round 1, nothing else ran.
        # Must NOT read as all_resolved — the adversarial check never happened.
        verdict, unresolved = compute_verdict([], attacker_failed=True)
        self.assertEqual(verdict, "attacker_failed")
        self.assertEqual(unresolved, [])

    def test_attacker_failed_ranks_below_unresolved(self):
        # A completed round with unresolved findings carries concrete work;
        # a later attacker failure must not mask it.
        rounds = [
            {
                "round": 1,
                "findings": [{"id": "ATK-001"}],
                "responses": [{"finding_ref": "ATK-001", "action": "deferred"}],
                "resolutions": {"ATK-001": "deferred"},
            }
        ]
        verdict, unresolved = compute_verdict(rounds, attacker_failed=True)
        self.assertEqual(verdict, "has_unresolved")
        self.assertEqual(unresolved, ["ATK-001"])

    def test_default_keeps_old_behaviour(self):
        verdict, _ = compute_verdict([])
        self.assertEqual(verdict, "all_resolved")


def _fake_cfg(section, key, default=None):
    return {"timeout": 900, "attacker_model": "opus", "defender_model": "sonnet"}.get(key, default)


class TimeoutRetryBudgetTests(unittest.TestCase):
    """A timed-out claude call is retried with double the budget, not
    identical parameters (which just timed out again, twice per night)."""

    def test_attacker_timeout_doubles_second_budget(self):
        calls = []

        def fake_run_claude(prompt, model=None, timeout=None):
            calls.append(timeout)
            return -1, "", f"claude subprocess timed out after {timeout}s"

        with (
            mock.patch.object(auto_harden, "run_claude", fake_run_claude),
            mock.patch.object(auto_harden, "cfg", _fake_cfg),
        ):
            result = auto_harden.run_attacker(["a.py"], {})
        self.assertIsNone(result)
        self.assertEqual(calls, [900, 1800])

    def test_attacker_non_timeout_failure_keeps_budget(self):
        calls = []

        def fake_run_claude(prompt, model=None, timeout=None):
            calls.append(timeout)
            return 1, "", "some non-timeout failure"

        with (
            mock.patch.object(auto_harden, "run_claude", fake_run_claude),
            mock.patch.object(auto_harden, "cfg", _fake_cfg),
        ):
            result = auto_harden.run_attacker(["a.py"], {})
        self.assertIsNone(result)
        self.assertEqual(calls, [900, 900])

    def test_defender_timeout_doubles_second_budget(self):
        calls = []

        def fake_run_claude(prompt, model=None, timeout=None):
            calls.append(timeout)
            return -1, "", f"claude subprocess timed out after {timeout}s"

        with (
            mock.patch.object(auto_harden, "run_claude", fake_run_claude),
            mock.patch.object(auto_harden, "cfg", _fake_cfg),
        ):
            result = auto_harden.run_defender([{"id": "ATK-001"}], ["a.py"])
        self.assertIsNone(result)
        self.assertEqual(calls, [900, 1800])


if __name__ == "__main__":
    unittest.main()
