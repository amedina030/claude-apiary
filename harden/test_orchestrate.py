#!/usr/bin/env python3
"""Tests for harden/orchestrate.py — the /harden control flow.

Hermetic: every test runs in its own tempdir, ``HARDEN_TMP_DIR`` redirects the
run state away from ``harden/tmp/``, git operations happen in a throwaway repo,
and the scribe launcher is replaced by a recording stub. The validators are
exercised as *real* subprocesses (orchestrate shells out to
``validate_and_assign.py``) exactly like ``test_validators.py`` does. No
``claude`` process is ever spawned.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "orchestrate.py")
PYTHON = sys.executable


def run(args: list, tmp_dir: str, cwd: str = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HARDEN_TMP_DIR": tmp_dir}
    return subprocess.run(
        [PYTHON, SCRIPT, *args],
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
        encoding="utf-8",
        errors="replace",
    )


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def make_repo(root: Path) -> Path:
    """A throwaway git repo with two committed source files."""
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (repo / "src" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (repo / "src" / "notes.md").write_text("not code\n", encoding="utf-8")
    git(str(repo), "init", "-q")
    git(str(repo), "config", "user.email", "t@example.com")
    git(str(repo), "config", "user.name", "t")
    git(str(repo), "add", "-A")
    git(str(repo), "commit", "-q", "-m", "init")
    return repo


def make_launcher(root: Path, record: Path, exit_code: int = 0) -> Path:
    """A stub standing in for .claude/apiary/launch.py — records argv, never runs scribe."""
    launcher = root / "launch_stub.py"
    launcher.write_text(
        "import json, sys\n"
        f"record = {str(record)!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('note added')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return launcher


class OrchestrateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.state = str(self.root / "state")
        Path(self.state).mkdir()
        self.repo = make_repo(self.root)

    def tearDown(self):
        # Windows keeps handles on git worktree dirs; ignore cleanup failures.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def plan(self, *extra, session="sess1234", targets=("src/a.py",), expect_ok=True):
        args = [
            "plan",
            "--session-id",
            session,
            "--repo",
            str(self.repo),
            "--cwd",
            str(self.repo),
            "--json",
        ]
        if targets:
            args += ["--targets", *targets]
        args += list(extra)
        result = run(args, self.state, cwd=str(self.repo))
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        return result

    def write(self, name: str, payload) -> str:
        path = self.root / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return str(path)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


class TestPlan(OrchestrateTestCase):
    def test_defaults_to_multi_lens_with_all_seven_lenses(self):
        plan = self.plan()
        self.assertEqual(plan["path"], "multi-lens")
        self.assertEqual(len(plan["resolved_lenses"]), 7)
        self.assertEqual(plan["mode"], "code")
        self.assertEqual(plan["focus"], "general")

    def test_single_lens_path_skips_the_referee(self):
        plan = self.plan("--lenses", "security")
        self.assertEqual(plan["path"], "single-lens")
        self.assertEqual(plan["resolved_lenses"], ["security"])

    def test_explicit_focus_without_lenses_selects_legacy(self):
        plan = self.plan("--focus", "security")
        self.assertEqual(plan["path"], "legacy")
        self.assertEqual(plan["resolved_lenses"], [])
        self.assertEqual(plan["focus"], "security")

    def test_focus_plus_lenses_stays_on_the_lens_path(self):
        plan = self.plan("--focus", "security", "--lenses", "correctness,testing")
        self.assertEqual(plan["path"], "multi-lens")
        self.assertEqual(plan["resolved_lenses"], ["correctness", "testing"])

    def test_lenses_are_deduped_and_lowercased_in_order(self):
        plan = self.plan("--lenses", "Security,correctness,SECURITY")
        self.assertEqual(plan["resolved_lenses"], ["security", "correctness"])

    def test_unknown_lens_aborts_before_any_state(self):
        result = self.plan("--lenses", "bogus", expect_ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown lens `bogus`", result.stderr)
        self.assertFalse(list(Path(self.state).glob("plan_*.json")))

    def test_directory_expansion_skips_tests_and_non_code(self):
        plan = self.plan(targets=("src",))
        self.assertEqual(sorted(plan["targets_rel"]), ["src/a.py", "src/b.py"])

    def test_directory_expansion_skips_excluded_dirs(self):
        cache = self.repo / "src" / "__pycache__"
        cache.mkdir()
        (cache / "junk.py").write_text("x = 1\n", encoding="utf-8")
        plan = self.plan(targets=("src",))
        self.assertNotIn("src/__pycache__/junk.py", plan["targets_rel"])

    def test_duplicate_targets_are_deduped(self):
        plan = self.plan(targets=("src", "src/a.py"))
        self.assertEqual(len(plan["targets_rel"]), len(set(plan["targets_rel"])))

    def test_empty_directory_gets_the_dedicated_message(self):
        (self.repo / "empty").mkdir()
        result = self.plan(targets=("empty",), expect_ok=False)
        self.assertIn("No code files found in `empty`", result.stderr)

    def test_too_many_files_aborts(self):
        result = self.plan("--max-files", "1", targets=("src",), expect_ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Too many files (2 > 1)", result.stderr)

    def test_missing_file_aborts(self):
        result = self.plan(targets=("src/nope.py",), expect_ok=False)
        self.assertIn("not found", result.stderr)

    def test_size_cap_aborts_without_creating_state(self):
        (self.repo / "big.py").write_text("# pad\n" * 5000, encoding="utf-8")
        result = self.plan("--max-target-kb", "1", targets=("big.py",), expect_ok=False)
        self.assertIn("exceeds --max-target-kb 1", result.stderr)
        self.assertFalse(list(Path(self.state).glob("plan_*.json")))

    def test_cost_estimate_matches_the_documented_formula(self):
        plan = self.plan("--rounds", "2", "--lenses", "security,testing")
        per_call = 15000 + 1.5 * plan["total_kb"] * 256
        self.assertEqual(plan["calls_per_round"], 4)  # 2 lenses + consolidator + defender
        self.assertEqual(plan["estimated_tokens"], int(2 * 4 * per_call))

    def test_legacy_path_costs_two_calls_per_round(self):
        plan = self.plan("--focus", "security", "--rounds", "3")
        self.assertEqual(plan["calls_per_round"], 2)

    def test_budget_warning_when_estimate_exceeds_budget(self):
        plan = self.plan("--budget-tokens", "100")
        self.assertIn("WARNING: Estimated cost", plan["budget_warning"])
        self.assertIn("WARNING: Estimated cost", plan["summary"])

    def test_no_budget_warning_when_under(self):
        self.assertIsNone(self.plan()["budget_warning"])

    def test_request_id_shape(self):
        plan = self.plan(session="abcd1234-1111-2222-3333-444455556666")
        parts = plan["request_id"].split("-")
        self.assertEqual(parts[0], "harden")
        self.assertEqual(parts[1], "abcd1234")
        self.assertTrue(parts[2].isdigit())
        self.assertEqual(len(parts[3]), 4)

    def test_worktree_names_are_derived_from_the_session(self):
        plan = self.plan(session="sid99")
        self.assertEqual(plan["worktree_path"], ".claude/worktrees/harden-sid99")
        self.assertEqual(plan["worktree_branch"], "harden-sid99")

    def test_plan_file_is_written_for_later_subcommands(self):
        self.plan(session="sess1234")
        self.assertTrue((Path(self.state) / "plan_sess1234.json").is_file())

    def test_human_summary_is_printed_without_json(self):
        result = run(
            [
                "plan",
                "--session-id",
                "s1",
                "--repo",
                str(self.repo),
                "--cwd",
                str(self.repo),
                "--targets",
                "src/a.py",
            ],
            self.state,
            cwd=str(self.repo),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("**Harden configuration:**", result.stdout)
        self.assertIn("Plan written to", result.stdout)

    def test_no_targets_aborts(self):
        result = run(
            ["plan", "--session-id", "s1", "--repo", str(self.repo), "--cwd", str(self.repo)],
            self.state,
            cwd=str(self.repo),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("No target given", result.stderr)

    def test_plan_mode_uses_the_legacy_path_and_the_note_body(self):
        record = self.root / "calls.jsonl"
        launcher = make_launcher(self.root, record)
        plan = self.plan(
            "--plan-note", "42", "--launcher", str(launcher), "--lenses", "security", targets=()
        )
        self.assertEqual(plan["mode"], "plan")
        self.assertEqual(plan["path"], "legacy")
        self.assertIsNone(plan["worktree_path"])
        self.assertIn("note added", plan["plan_note_content"])
        self.assertTrue(any("--lenses ignored" in n for n in plan["notes"]))
        calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0][:3], ["scribe/notes.py", "get", "42"])

    def test_missing_note_aborts(self):
        launcher = make_launcher(self.root, self.root / "calls.jsonl", exit_code=1)
        result = self.plan(
            "--plan-note", "9999", "--launcher", str(launcher), targets=(), expect_ok=False
        )
        self.assertIn("Note 9999 not found", result.stderr)


# --------------------------------------------------------------------------- #
# worktree
# --------------------------------------------------------------------------- #


class TestWorktree(OrchestrateTestCase):
    def test_check_passes_on_clean_targets(self):
        self.plan()
        result = run(["worktree", "check", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean at HEAD", result.stdout)

    def test_check_aborts_on_a_dirty_target(self):
        self.plan()
        (self.repo / "src" / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
        result = run(["worktree", "check", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 1)
        self.assertIn("has uncommitted changes or is untracked", result.stderr)

    def test_check_aborts_on_an_untracked_target(self):
        (self.repo / "src" / "c.py").write_text("def c():\n    pass\n", encoding="utf-8")
        self.plan(targets=("src/c.py",))
        result = run(["worktree", "check", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 1)
        self.assertIn("has uncommitted changes or is untracked", result.stderr)

    def test_create_makes_the_branch_and_directory(self):
        plan = self.plan()
        result = run(["worktree", "create", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / plan["worktree_path"] / "src" / "a.py").is_file())
        branches = git(str(self.repo), "branch", "--list", plan["worktree_branch"]).stdout
        self.assertIn(plan["worktree_branch"], branches)

    def test_create_refuses_when_a_target_is_dirty(self):
        plan = self.plan()
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        result = run(["worktree", "create", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.repo / plan["worktree_path"]).exists())

    def test_diff_reports_no_edits_when_the_worktree_is_untouched(self):
        self.plan()
        run(["worktree", "create", "--session-id", "sess1234"], self.state)
        result = run(["worktree", "diff", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Defenders did not make any file edits", result.stdout)

    def test_diff_shows_defender_edits(self):
        plan = self.plan()
        run(["worktree", "create", "--session-id", "sess1234"], self.state)
        edited = self.repo / plan["worktree_path"] / "src" / "a.py"
        edited.write_text("def a():\n    return 42\n", encoding="utf-8")
        result = run(["worktree", "diff", "--session-id", "sess1234"], self.state)
        self.assertIn("return 42", result.stdout)

    def test_remove_keeps_the_branch_by_default(self):
        plan = self.plan()
        run(["worktree", "create", "--session-id", "sess1234"], self.state)
        result = run(["worktree", "remove", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / plan["worktree_path"]).exists())
        self.assertIn(
            plan["worktree_branch"],
            git(str(self.repo), "branch", "--list", plan["worktree_branch"]).stdout,
        )

    def test_remove_with_delete_branch_drops_the_branch(self):
        plan = self.plan()
        run(["worktree", "create", "--session-id", "sess1234"], self.state)
        result = run(
            ["worktree", "remove", "--session-id", "sess1234", "--delete-branch"], self.state
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            git(str(self.repo), "branch", "--list", plan["worktree_branch"]).stdout.strip(), ""
        )

    def test_plan_mode_has_no_worktree(self):
        launcher = make_launcher(self.root, self.root / "calls.jsonl")
        self.plan("--plan-note", "7", "--launcher", str(launcher), targets=())
        result = run(["worktree", "create", "--session-id", "sess1234"], self.state)
        self.assertEqual(result.returncode, 0)
        self.assertIn("plan mode: no worktree", result.stdout)

    def test_missing_plan_aborts_with_guidance(self):
        result = run(["worktree", "check", "--session-id", "nothere"], self.state)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no plan for session", result.stderr)


# --------------------------------------------------------------------------- #
# round
# --------------------------------------------------------------------------- #


class TestRound(OrchestrateTestCase):
    def test_start_tick_status_reset(self):
        self.assertEqual(
            run(["round", "start", "--session-id", "s"], self.state).stdout.strip(), "0"
        )
        self.assertEqual(
            run(["round", "tick", "--session-id", "s"], self.state).stdout.strip(), "1"
        )
        self.assertEqual(
            run(["round", "tick", "--session-id", "s"], self.state).stdout.strip(), "2"
        )
        self.assertEqual(
            run(["round", "status", "--session-id", "s"], self.state).stdout.strip(), "2"
        )
        self.assertEqual(
            run(["round", "reset", "--session-id", "s"], self.state).stdout.strip(), "0"
        )

    def test_defender_set_then_get(self):
        run(["round", "start", "--session-id", "s"], self.state)
        run(["round", "defender", "--session-id", "s", "--set", "agent-77"], self.state)
        result = run(["round", "defender", "--session-id", "s", "--get"], self.state)
        self.assertEqual(result.stdout.strip(), "agent-77")

    def test_defender_get_without_a_stored_id_fails(self):
        run(["round", "start", "--session-id", "s"], self.state)
        result = run(["round", "defender", "--session-id", "s", "--get"], self.state)
        self.assertEqual(result.returncode, 1)

    def test_defender_needs_set_or_get(self):
        result = run(["round", "defender", "--session-id", "s"], self.state)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--set", result.stderr)


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #


def make_findings(*ids):
    return [
        {"id": i, "severity": "high", "description": f"issue {i}", "location": "src/a.py:1-2"}
        for i in ids
    ]


class TestPrompt(OrchestrateTestCase):
    def test_lens_attacker_prompts_one_block_per_lens(self):
        self.plan("--lenses", "security,correctness")
        result = run(["prompt", "attacker", "--session-id", "sess1234", "--round", "1"], self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("--- PROMPT BEGIN ---"), 2)
        self.assertIn("Harden Attacker security round 1 [rid:harden-", result.stdout)
        self.assertIn("Harden Attacker correctness round 1 [rid:harden-", result.stdout)
        self.assertIn("in ONE message so they run in parallel", result.stdout)

    def test_lens_attacker_prompt_carries_brief_and_seam_rules(self):
        self.plan("--lenses", "security")
        out = run(["prompt", "attacker", "--session-id", "sess1234"], self.state).stdout
        self.assertNotIn("{{", out)
        self.assertIn("Threat model = a hostile actor", out)
        self.assertIn("security vs robustness", out)

    def test_round_one_uses_original_paths(self):
        self.plan("--lenses", "security")
        out = run(
            ["prompt", "attacker", "--session-id", "sess1234", "--round", "1"], self.state
        ).stdout
        self.assertIn("- src/a.py", out)
        self.assertNotIn(".claude/worktrees/harden-sess1234/src/a.py", out)

    def test_round_two_attacker_reads_the_worktree(self):
        self.plan("--lenses", "security")
        out = run(
            ["prompt", "attacker", "--session-id", "sess1234", "--round", "2"], self.state
        ).stdout
        self.assertIn(".claude/worktrees/harden-sess1234/src/a.py", out)
        self.assertIn("ORIGINAL relative file paths", out)

    def test_lens_subset_flag_limits_the_fan_out(self):
        self.plan()
        out = run(
            ["prompt", "attacker", "--session-id", "sess1234", "--lens", "testing"], self.state
        ).stdout
        self.assertEqual(out.count("--- PROMPT BEGIN ---"), 1)
        self.assertIn("Harden Attacker testing round 1", out)

    def test_unknown_lens_override_aborts(self):
        self.plan()
        result = run(
            ["prompt", "attacker", "--session-id", "sess1234", "--lens", "nope"], self.state
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown lens `nope`", result.stderr)

    def test_legacy_attacker_uses_the_focus_template(self):
        self.plan("--focus", "security")
        out = run(["prompt", "attacker", "--session-id", "sess1234"], self.state).stdout
        self.assertIn("Harden Attacker round 1 [rid:harden-", out)
        self.assertIn("**Focus:** security", out)
        self.assertIn("None (first round)", out)
        self.assertNotIn("{{", out)

    def test_legacy_attacker_round_two_sees_the_defender_json(self):
        self.plan("--focus", "security")
        prev = self.write(
            "prev.json",
            {
                "responses": [
                    {
                        "id": "DEF-001",
                        "finding_ref": "ATK-001",
                        "action": "fixed",
                        "description": "did it",
                    }
                ]
            },
        )
        out = run(
            [
                "prompt",
                "attacker",
                "--session-id",
                "sess1234",
                "--round",
                "2",
                "--prev-response",
                prev,
            ],
            self.state,
        ).stdout
        self.assertIn("ATK-001", out)
        self.assertNotIn("None (first round)", out)

    def test_consolidator_prompt_embeds_the_merged_findings(self):
        self.plan("--lenses", "security,correctness")
        findings = self.write("f.json", make_findings("ATK-SEC-001", "ATK-COR-001"))
        out = run(
            ["prompt", "consolidator", "--session-id", "sess1234", "--findings", findings],
            self.state,
        ).stdout
        self.assertIn("Harden Consolidator round 1 [rid:harden-", out)
        self.assertIn("ATK-SEC-001", out)
        self.assertNotIn("{{", out)

    def test_prior_record_is_built_mechanically(self):
        self.plan("--lenses", "security,correctness")
        prev_f = self.write("pf.json", make_findings("CON-001", "CON-002"))
        prev_r = self.write(
            "pr.json",
            {
                "responses": [
                    {
                        "id": "DEF-001",
                        "finding_ref": "CON-001",
                        "action": "fixed",
                        "description": "x",
                    },
                    {
                        "id": "DEF-002",
                        "finding_ref": "CON-002",
                        "action": "deferred",
                        "description": "y",
                    },
                ]
            },
        )
        rej = self.write(
            "rej.json",
            {
                "accepted": [],
                "rejected": [{"source_ids": ["ATK-CPX-004", "ATK-ARC-002"], "reason": "dupe"}],
            },
        )
        out = run(
            [
                "prompt",
                "attacker",
                "--session-id",
                "sess1234",
                "--round",
                "2",
                "--lens",
                "security",
                "--prev-findings",
                prev_f,
                "--prev-response",
                prev_r,
                "--rejections",
                rej,
            ],
            self.state,
        ).stdout
        self.assertIn("CON-001 src/a.py:1-2 — fixed", out)
        self.assertIn("CON-002 src/a.py:1-2 — deferred", out)
        self.assertIn("ATK-CPX-004, ATK-ARC-002 — dupe", out)

    def test_prior_record_falls_back_to_a_rejection_id(self):
        self.plan("--lenses", "security")
        rej = self.write(
            "rej.json", {"accepted": [], "rejected": [{"id": "CON-009", "reason": "stale"}]}
        )
        out = run(
            [
                "prompt",
                "attacker",
                "--session-id",
                "sess1234",
                "--round",
                "2",
                "--lens",
                "security",
                "--rejections",
                rej,
            ],
            self.state,
        ).stdout
        self.assertIn("CON-009 — stale", out)

    def test_defender_prompt_points_at_worktree_paths(self):
        self.plan("--lenses", "security")
        findings = self.write("f.json", make_findings("ATK-SEC-001"))
        out = run(
            ["prompt", "defender", "--session-id", "sess1234", "--findings", findings], self.state
        ).stdout
        self.assertIn("Harden Defender round 1 [rid:harden-", out)
        self.assertIn(".claude/worktrees/harden-sess1234/src/a.py", out)
        self.assertIn("WORKFLOW:", out)
        self.assertIn("Do NOT pass `isolation`", out)
        self.assertNotIn("{{", out)

    def test_defender_continue_message_has_mechanical_counts(self):
        self.plan("--lenses", "security")
        findings = self.write("f.json", make_findings("CON-003", "CON-004"))
        prev = self.write(
            "pr.json",
            {
                "responses": [
                    {
                        "id": "DEF-001",
                        "finding_ref": "CON-001",
                        "action": "fixed",
                        "description": "x",
                    },
                    {
                        "id": "DEF-002",
                        "finding_ref": "CON-002",
                        "action": "deferred",
                        "description": "y",
                    },
                ]
            },
        )
        out = run(
            [
                "prompt",
                "defender-continue",
                "--session-id",
                "sess1234",
                "--round",
                "2",
                "--findings",
                findings,
                "--prev-response",
                prev,
            ],
            self.state,
        ).stdout
        self.assertIn("## Round 2 Findings", out)
        self.assertIn("found 2 new issues", out)
        self.assertIn("- Fixed: 1 (CON-001)", out)
        self.assertIn("- Deferred: 1 (CON-002)", out)
        self.assertIn("SENDMESSAGE", out)

    def test_findings_are_required_for_the_defender_and_referee(self):
        self.plan("--lenses", "security,correctness")
        for role in ("consolidator", "defender", "defender-continue"):
            result = run(["prompt", role, "--session-id", "sess1234"], self.state)
            self.assertEqual(result.returncode, 1, role)
            self.assertIn("needs --findings", result.stderr)

    def test_plan_mode_defender_gets_the_note_body(self):
        launcher = make_launcher(self.root, self.root / "calls.jsonl")
        self.plan("--plan-note", "5", "--launcher", str(launcher), targets=())
        findings = self.write("f.json", make_findings("ATK-001"))
        out = run(
            ["prompt", "defender", "--session-id", "sess1234", "--findings", findings], self.state
        ).stdout
        self.assertIn("note added", out)
        self.assertNotIn("worktrees", out)


# --------------------------------------------------------------------------- #
# validate — the retry / drop / ask / degrade policy
# --------------------------------------------------------------------------- #

VALID_LENS_FINDING = [
    {"severity": "high", "description": "Unbounded recursion", "location": "src/a.py:1-2"}
]
VALID_LEGACY_FINDING = [
    {
        "category": "security",
        "severity": "high",
        "description": "SQLi in the query builder",
        "location": "src/a.py:1-2",
    }
]


class TestValidate(OrchestrateTestCase):
    def decide(self, *args):
        result = run(["validate", *args], self.state, cwd=str(self.repo))
        return result, json.loads(result.stdout)

    def test_valid_lens_findings_get_ids_and_an_out_file(self):
        self.plan("--lenses", "security")
        raw = self.write("out.json", VALID_LENS_FINDING)
        result, decision = self.decide(
            "findings", "--file", raw, "--session-id", "sess1234", "--lens", "security"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(decision["status"], "ok")
        self.assertEqual(decision["result"][0]["id"], "ATK-SEC-001")
        self.assertTrue(Path(decision["out_file"]).is_file())

    def test_findings_decision_carries_a_severity_tally(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json",
            [
                {"severity": "critical", "description": "boom", "location": "src/a.py:1"},
                {"severity": "low", "description": "nit", "location": "src/a.py:2"},
                {"severity": "low", "description": "nit two", "location": "src/b.py:1"},
            ],
        )
        _, decision = self.decide(
            "findings", "--file", raw, "--session-id", "sess1234", "--lens", "security"
        )
        self.assertEqual(
            decision["counts"], {"total": 3, "critical": 1, "high": 0, "medium": 0, "low": 2}
        )

    def test_response_decision_carries_an_action_tally(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json",
            {
                "responses": [
                    {"finding_ref": "ATK-SEC-001", "action": "fixed", "description": "patched"},
                    {"finding_ref": "ATK-SEC-002", "action": "deferred", "description": "later"},
                ]
            },
        )
        _, decision = self.decide(
            "response",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--expected-ids",
            "ATK-SEC-001,ATK-SEC-002",
        )
        self.assertEqual(
            decision["counts"], {"total": 2, "fixed": 1, "refactored": 0, "deferred": 1}
        )

    def test_consolidation_decision_counts_rejections(self):
        self.plan("--lenses", "security,correctness")
        raw = self.write(
            "out.json",
            {
                "accepted": [
                    {
                        "severity": "high",
                        "description": "merged",
                        "location": "src/a.py:1-2",
                        "source_ids": ["ATK-SEC-001"],
                        "lenses": ["security"],
                    }
                ],
                "rejected": [{"source_ids": ["ATK-COR-001"], "reason": "duplicate"}],
            },
        )
        result, decision = self.decide(
            "consolidation",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--source-ids",
            "ATK-SEC-001,ATK-COR-001",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(decision["counts"]["total"], 1)
        self.assertEqual(decision["counts"]["rejected"], 1)
        self.assertEqual(decision["counts"]["high"], 1)

    def test_markdown_fences_are_stripped_before_validation(self):
        self.plan("--lenses", "security")
        raw = self.write("out.json", "```json\n" + json.dumps(VALID_LENS_FINDING) + "\n```")
        result, decision = self.decide(
            "findings", "--file", raw, "--session-id", "sess1234", "--lens", "security"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(decision["status"], "ok")

    def test_first_lens_failure_asks_for_a_retry(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json", [{"severity": "nope", "description": "", "location": "src/a.py:1"}]
        )
        result, decision = self.decide(
            "findings",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--lens",
            "security",
            "--attempt",
            "1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(decision["status"], "retry")
        self.assertIn("failed validation", decision["feedback"])
        self.assertIn("--attempt 2", decision["instruction"])

    def test_second_lens_failure_drops_that_lens(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json", [{"severity": "nope", "description": "", "location": "src/a.py:1"}]
        )
        result, decision = self.decide(
            "findings",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--lens",
            "security",
            "--attempt",
            "2",
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(decision["status"], "drop")
        self.assertIn("lens security: dropped (unparseable output)", decision["instruction"])
        self.assertIsNone(decision["feedback"])

    def test_second_legacy_findings_failure_asks_the_user(self):
        self.plan("--focus", "security")
        raw = self.write(
            "out.json",
            [
                {
                    "category": "bogus",
                    "severity": "high",
                    "description": "x",
                    "location": "src/a.py:1",
                }
            ],
        )
        result, decision = self.decide(
            "findings", "--file", raw, "--session-id", "sess1234", "--attempt", "2"
        )
        self.assertEqual(decision["status"], "ask")
        self.assertIn("plain prose", decision["instruction"])
        self.assertIn("Do not use a multiple-choice picker", decision["instruction"])

    def test_sanitize_is_always_on_for_findings(self):
        self.plan("--focus", "security")
        raw = self.write("out.json", [dict(VALID_LEGACY_FINDING[0], title="stray field")])
        result, decision = self.decide("findings", "--file", raw, "--session-id", "sess1234")
        self.assertEqual(decision["status"], "ok")
        self.assertNotIn("title", decision["result"][0])

    def test_response_needs_expected_ids(self):
        self.plan("--lenses", "security")
        raw = self.write("out.json", {"responses": []})
        result = run(
            ["validate", "response", "--file", raw, "--session-id", "sess1234"],
            self.state,
            cwd=str(self.repo),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--expected-ids", result.stderr)

    def test_valid_response_gets_def_ids(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json",
            {
                "responses": [
                    {"finding_ref": "ATK-SEC-001", "action": "fixed", "description": "patched"}
                ]
            },
        )
        result, decision = self.decide(
            "response", "--file", raw, "--session-id", "sess1234", "--expected-ids", "ATK-SEC-001"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(decision["result"]["responses"][0]["id"], "DEF-001")

    def test_response_failure_escalates_to_ask_on_the_retry(self):
        self.plan("--lenses", "security")
        raw = self.write("out.json", {"responses": []})
        result, decision = self.decide(
            "response",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--expected-ids",
            "ATK-SEC-001",
            "--attempt",
            "2",
        )
        self.assertEqual(decision["status"], "ask")

    def test_consolidation_failure_degrades_on_the_retry(self):
        self.plan("--lenses", "security,correctness")
        raw = self.write("out.json", {"accepted": [], "rejected": []})
        result, decision = self.decide(
            "consolidation",
            "--file",
            raw,
            "--session-id",
            "sess1234",
            "--source-ids",
            "ATK-SEC-001",
            "--attempt",
            "2",
        )
        self.assertEqual(decision["status"], "degrade")
        self.assertIn("--degrade", decision["instruction"])

    def test_degrade_dedups_and_assigns_con_ids(self):
        self.plan("--lenses", "security,correctness")
        merged = self.write(
            "merged.json",
            [
                {
                    "id": "ATK-SEC-001",
                    "severity": "high",
                    "description": "same spot",
                    "location": "src/a.py:1-2",
                    "lens": "security",
                },
                {
                    "id": "ATK-COR-001",
                    "severity": "low",
                    "description": "same spot",
                    "location": "src/a.py:1-2",
                    "lens": "correctness",
                },
            ],
        )
        result, decision = self.decide(
            "consolidation", "--file", merged, "--session-id", "sess1234", "--degrade"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(decision["degraded"])
        self.assertEqual(len(decision["result"]["accepted"]), 1)
        self.assertEqual(decision["result"]["accepted"][0]["id"], "CON-001")

    def test_check_files_is_implied_by_code_mode(self):
        self.plan("--lenses", "security")
        raw = self.write(
            "out.json",
            [{"severity": "high", "description": "ghost file", "location": "src/ghost.py:1-2"}],
        )
        result, decision = self.decide(
            "findings", "--file", raw, "--session-id", "sess1234", "--lens", "security"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("ghost.py", decision["errors"])

    def test_missing_output_file_is_a_hard_error(self):
        self.plan("--lenses", "security")
        result = run(
            [
                "validate",
                "findings",
                "--file",
                str(self.root / "nope.json"),
                "--session-id",
                "sess1234",
            ],
            self.state,
            cwd=str(self.repo),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_works_without_a_plan_using_explicit_flags(self):
        raw = self.write("out.json", VALID_LENS_FINDING)
        result = run(
            ["validate", "findings", "--file", raw, "--lens", "security"],
            self.state,
            cwd=str(self.repo),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


class TestBudget(OrchestrateTestCase):
    def check(self, *args):
        result = run(["budget", "check", *args], self.state)
        return result, json.loads(result.stdout)

    def test_known_spend_under_budget(self):
        self.plan("--budget-tokens", "1000")
        _, payload = self.check("--session-id", "sess1234", "--spent", "250")
        self.assertFalse(payload["exceeded"])
        self.assertEqual(payload["pct"], 25)
        self.assertEqual(payload["suffix"], "| spent 250 of 1000 (25%)")
        self.assertIsNone(payload["abort_message"])

    def test_overrun_produces_the_abort_message(self):
        self.plan("--budget-tokens", "1000")
        _, payload = self.check("--session-id", "sess1234", "--spent", "1500", "--round", "2")
        self.assertTrue(payload["exceeded"])
        self.assertIn("BUDGET EXCEEDED: spent 1500 > budget 1000", payload["abort_message"])
        self.assertIn("Aborting after round 2", payload["abort_message"])
        self.assertIn("Do NOT remove the worktree", payload["instruction"])

    def test_empty_findings_exit_never_marks_budget_exceeded(self):
        self.plan("--budget-tokens", "1000")
        _, payload = self.check("--session-id", "sess1234", "--spent", "1500", "--empty-findings")
        self.assertFalse(payload["exceeded"])
        self.assertIsNone(payload["abort_message"])

    def test_query_script_returning_an_integer_is_used(self):
        self.plan("--budget-tokens", "1000")
        script = self.root / "fake_query.py"
        script.write_text("print(777)\n", encoding="utf-8")
        _, payload = self.check("--session-id", "sess1234", "--query-script", str(script))
        self.assertEqual(payload["spent"], 777)

    def test_query_script_failure_yields_unknown_and_no_abort(self):
        self.plan("--budget-tokens", "1000")
        script = self.root / "fake_query.py"
        script.write_text(
            "import sys\nsys.stderr.write('log missing')\nsys.exit(2)\n", encoding="utf-8"
        )
        _, payload = self.check("--session-id", "sess1234", "--query-script", str(script))
        self.assertIsNone(payload["spent"])
        self.assertFalse(payload["exceeded"])
        self.assertIn("| spent: unknown (log missing)", payload["suffix"])

    def test_non_integer_output_is_treated_as_unknown(self):
        self.plan("--budget-tokens", "1000")
        script = self.root / "fake_query.py"
        script.write_text("print('123 tokens-ish')\n", encoding="utf-8")
        _, payload = self.check("--session-id", "sess1234", "--query-script", str(script))
        self.assertIsNone(payload["spent"])

    def test_error_text_is_trimmed_to_eighty_chars(self):
        self.plan("--budget-tokens", "1000")
        script = self.root / "fake_query.py"
        script.write_text(
            "import sys\nsys.stderr.write('x' * 500)\nsys.exit(2)\n", encoding="utf-8"
        )
        _, payload = self.check("--session-id", "sess1234", "--query-script", str(script))
        self.assertEqual(len(payload["error"]), 80)

    def test_zero_budget_does_not_divide_by_zero(self):
        self.plan("--budget-tokens", "1000")
        _, payload = self.check("--session-id", "sess1234", "--spent", "5", "--budget", "0")
        self.assertIsNone(payload["pct"])
        self.assertIn("(n/a)", payload["suffix"])

    def test_budget_override_wins_over_the_plan(self):
        self.plan("--budget-tokens", "1000")
        _, payload = self.check("--session-id", "sess1234", "--spent", "50", "--budget", "100")
        self.assertEqual(payload["budget"], 100)
        self.assertEqual(payload["pct"], 50)


# --------------------------------------------------------------------------- #
# file-todos / save-summary
# --------------------------------------------------------------------------- #

RESPONSE_WITH_DEFERRAL = {
    "responses": [
        {"id": "DEF-001", "finding_ref": "CON-001", "action": "fixed", "description": "patched"},
        {
            "id": "DEF-002",
            "finding_ref": "CON-002",
            "action": "deferred",
            "description": "needs a schema migration",
        },
    ],
    "todos": ["Add a regression test for the parser"],
}


class TestFileTodos(OrchestrateTestCase):
    def setUp(self):
        super().setUp()
        self.record = self.root / "calls.jsonl"
        self.launcher = make_launcher(self.root, self.record)

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def test_files_both_defender_todos_and_deferred_findings(self):
        self.plan()
        findings = self.write(
            "f.json",
            [
                {
                    "id": "CON-002",
                    "severity": "medium",
                    "description": "unchecked index",
                    "location": "src/a.py:1-2",
                }
            ],
        )
        result = run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--round",
                "2",
                "--response",
                self.write("r.json", RESPONSE_WITH_DEFERRAL),
                "--findings",
                findings,
                "--launcher",
                str(self.launcher),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["filed"], 2)
        self.assertEqual(payload["failed"], [])
        self.assertIn(
            "Add a regression test for the parser (from /harden round 2)", payload["todos"]
        )
        self.assertIn(
            "Deferred CON-002: unchecked index — Reason: needs a schema "
            "migration (from /harden round 2)",
            payload["todos"],
        )

    def test_content_goes_through_content_file_not_argv(self):
        self.plan()
        run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--round",
                "1",
                "--response",
                self.write("r.json", RESPONSE_WITH_DEFERRAL),
                "--launcher",
                str(self.launcher),
            ],
            self.state,
        )
        for call in self.calls():
            self.assertIn("--content-file", call)
            self.assertNotIn("--content", call)
            self.assertIn("--auto", call)
            self.assertEqual(call[:3], ["scribe/notes.py", "add", "--type"])

    def test_dry_run_writes_nothing(self):
        self.plan()
        result = run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--response",
                self.write("r.json", RESPONSE_WITH_DEFERRAL),
                "--launcher",
                str(self.launcher),
                "--dry-run",
            ],
            self.state,
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["filed"], 0)
        self.assertEqual(self.calls(), [])

    def test_nothing_to_file_is_a_clean_no_op(self):
        self.plan()
        result = run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--response",
                self.write("r.json", {"responses": [], "todos": []}),
                "--launcher",
                str(self.launcher),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["filed"], 0)

    def test_scribe_failure_is_reported_and_exits_non_zero(self):
        self.plan()
        failing = make_launcher(self.root, self.record, exit_code=1)
        result = run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--response",
                self.write("r.json", RESPONSE_WITH_DEFERRAL),
                "--launcher",
                str(failing),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(json.loads(result.stdout)["failed"]), 2)

    def test_missing_launcher_aborts(self):
        self.plan()
        result = run(
            [
                "file-todos",
                "--session-id",
                "sess1234",
                "--response",
                self.write("r.json", RESPONSE_WITH_DEFERRAL),
                "--launcher",
                str(self.root / "nope.py"),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("launcher not found", result.stderr)


class TestSaveSummary(OrchestrateTestCase):
    def test_saves_the_summary_as_a_context_note(self):
        self.plan()
        record = self.root / "calls.jsonl"
        launcher = make_launcher(self.root, record)
        summary = self.write("summary.md", "## /harden Summary\n\n**Target:** src/a.py\n")
        result = run(
            [
                "save-summary",
                "--session-id",
                "sess1234",
                "--content-file",
                summary,
                "--launcher",
                str(launcher),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["saved"])
        call = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(call[:4], ["scribe/notes.py", "add", "--type", "context"])
        self.assertIn("--content-file", call)
        self.assertNotIn("--auto", call)

    def test_dry_run_does_not_call_scribe(self):
        self.plan()
        record = self.root / "calls.jsonl"
        launcher = make_launcher(self.root, record)
        summary = self.write("summary.md", "body")
        result = run(
            [
                "save-summary",
                "--session-id",
                "sess1234",
                "--content-file",
                summary,
                "--launcher",
                str(launcher),
                "--dry-run",
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(record.exists())

    def test_missing_summary_file_aborts(self):
        self.plan()
        result = run(
            [
                "save-summary",
                "--session-id",
                "sess1234",
                "--content-file",
                str(self.root / "gone.md"),
            ],
            self.state,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("summary file not found", result.stderr)


# --------------------------------------------------------------------------- #
# CLI shape
# --------------------------------------------------------------------------- #

REPO = Path(__file__).resolve().parent.parent
HARDEN_MD = REPO / "harden" / "commands" / "harden.md"
WRAPUP_MD = REPO / "core" / "commands" / "wrapup.md"
REFINE_MD = REPO / "refiner" / "commands" / "refine.md"

# Phrases that only appear when a skill is doing the orchestrator's job in prose:
# retry ceilings, degrade fallbacks, the cost formula, the spend parsing, and the
# raw CLI calls orchestrate.py now owns. If one of these comes back, the logic
# has leaked out of Python again (review X-8).
ORCHESTRATION_PROSE = (
    "askuserquestion",
    "on validation failure",
    "if validation fails",
    "if the retry also fails",
    "retry up to",
    "re-spawn the attacker",
    "graceful degradation",
    "per-lens failure handling",
    "calls_per_round",
    "spent_status",
    "total_kb",
    "validate_and_assign.py",
    "git worktree add",
    "round_counter.py",
    "query_request.py",
    "__harden_size_check",
)


class TestSkillProse(unittest.TestCase):
    """/harden is a program; harden.md must not re-implement it in English."""

    def test_harden_md_has_no_orchestration_branching(self):
        text = HARDEN_MD.read_text(encoding="utf-8").lower()
        for phrase in ORCHESTRATION_PROSE:
            self.assertNotIn(
                phrase, text, f"harden.md still carries orchestration prose: {phrase!r}"
            )

    def test_harden_md_delegates_to_orchestrate(self):
        text = HARDEN_MD.read_text(encoding="utf-8")
        self.assertIn("harden/orchestrate.py", text)
        for verb in (
            "ORCH plan",
            "ORCH worktree",
            "ORCH round",
            "ORCH prompt",
            "ORCH validate",
            "ORCH budget",
            "ORCH file-todos",
            "ORCH save-summary",
        ):
            self.assertIn(verb, text, f"harden.md never calls {verb}")

    def test_harden_md_stays_short(self):
        lines = HARDEN_MD.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(
            len(lines), 150, "harden.md is drifting back toward a prose orchestrator"
        )

    def test_no_skill_mandates_the_multiple_choice_picker(self):
        for path in (HARDEN_MD, WRAPUP_MD, REFINE_MD):
            self.assertNotIn(
                "AskUserQuestion",
                path.read_text(encoding="utf-8"),
                f"{path.name} still mandates the picker",
            )

    def test_wrapup_step_four_delegates_to_classify(self):
        text = WRAPUP_MD.read_text(encoding="utf-8")
        self.assertIn("compass/classify.py <session_id_8char>", text)
        self.assertNotIn("compass/capture.py", text)
        self.assertNotIn("compass/observations.py validate", text)
        self.assertNotIn("compass/dimensions.json", text)


class TestCliShape(unittest.TestCase):
    def test_no_subcommand_prints_help_and_exits_one(self):
        result = subprocess.run(
            [PYTHON, SCRIPT], text=True, capture_output=True, encoding="utf-8", errors="replace"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stdout)

    def test_every_subcommand_is_reachable(self):
        result = subprocess.run(
            [PYTHON, SCRIPT, "--help"],
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        for name in (
            "plan",
            "prompt",
            "worktree",
            "round",
            "validate",
            "budget",
            "file-todos",
            "save-summary",
        ):
            self.assertIn(name, result.stdout)


if __name__ == "__main__":
    unittest.main()
