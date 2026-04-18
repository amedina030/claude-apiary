"""Tests for auto_harden.compute_verdict — the pure verdict-classification
function that distinguishes defender_failed from has_unresolved/all_resolved.
"""
import unittest

from runner.auto_harden import compute_verdict


class ComputeVerdictTests(unittest.TestCase):
    def test_clean_target_no_findings(self):
        rounds = [{"round": 1, "findings": [], "responses": [], "resolutions": {}}]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])

    def test_all_findings_fixed(self):
        rounds = [{
            "round": 1,
            "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
            "responses": [
                {"finding_ref": "ATK-001", "action": "fixed"},
                {"finding_ref": "ATK-002", "action": "refactored"},
            ],
            "resolutions": {"ATK-001": "fixed", "ATK-002": "refactored"},
        }]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])

    def test_has_unresolved_with_deferred(self):
        rounds = [{
            "round": 1,
            "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
            "responses": [
                {"finding_ref": "ATK-001", "action": "fixed"},
                {"finding_ref": "ATK-002", "action": "deferred"},
            ],
            "resolutions": {"ATK-001": "fixed", "ATK-002": "deferred"},
        }]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "has_unresolved")
        self.assertEqual(unresolved, ["ATK-002"])

    def test_defender_failed_none_response(self):
        # run_defender returned None — resolutions explicitly marked unresolved
        rounds = [{
            "round": 1,
            "findings": [{"id": "ATK-001"}, {"id": "ATK-002"}],
            "responses": [],
            "resolutions": {"ATK-001": "unresolved", "ATK-002": "unresolved"},
        }]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")
        self.assertEqual(sorted(unresolved), ["ATK-001", "ATK-002"])

    def test_defender_failed_empty_response_array(self):
        # run_defender returned [] — resolutions is empty (latent bug pre-fix).
        # compute_verdict must still flag this as defender_failed.
        rounds = [{
            "round": 1,
            "findings": [{"id": "ATK-001"}],
            "responses": [],
            "resolutions": {},
        }]
        verdict, unresolved = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")
        self.assertEqual(unresolved, [])

    def test_defender_failed_takes_priority_over_has_unresolved(self):
        # If any round had findings-without-responses, the whole run is flagged
        # defender_failed even if another round produced normal output. In the
        # runner (max_rounds=1) this path is unreachable, but the contract
        # should be clear for future multi-round use.
        rounds = [
            {"round": 1, "findings": [{"id": "ATK-001"}], "responses": [],
             "resolutions": {"ATK-001": "unresolved"}},
            {"round": 2, "findings": [{"id": "ATK-002"}],
             "responses": [{"finding_ref": "ATK-002", "action": "fixed"}],
             "resolutions": {"ATK-002": "fixed"}},
        ]
        verdict, _ = compute_verdict(rounds)
        self.assertEqual(verdict, "defender_failed")

    def test_empty_rounds_list(self):
        # Edge case: auto_harden aborted before any round completed
        verdict, unresolved = compute_verdict([])
        self.assertEqual(verdict, "all_resolved")
        self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
