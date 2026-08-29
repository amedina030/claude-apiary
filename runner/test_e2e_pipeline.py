#!/usr/bin/env python3
"""Hermetic end-to-end test for the six-stage runner pipeline.

This is the acceptance harness the deep review asked for (review §5a-G item 1,
`git show 5b95eaa:docs/review/subsystems/runner.md` §6: "There is no end-to-end test that runs
two real stage subprocesses back-to-back with a fake `claude` binary").

What is real here:
  * ``python -m runner.run`` is spawned as a subprocess, exactly as an operator
    or Task Scheduler would run it, and it spawns all six real stage modules.
  * A real git target repo with a real bare "origin" remote, a real worktree in
    detached mode, real branches and real commits.
  * A temp ``APIARY_TARGET_STATE_DIR`` so every artifact, lock, tracker and
    ``run_history.jsonl`` row lands in the tempdir and nothing touches the
    operator's state.

What is faked:
  * ``claude`` itself. A shim named ``claude`` (``claude.bat`` on Windows) is
    put first on ``PATH``; it reads the prompt on stdin, decides which stage is
    calling from the prompt's opening line, and answers from a canned scenario
    JSON — spec, plan, file writes, attacker findings, defender responses,
    approval deferral reviews. It also appends one line per call to a log so
    the test can assert which stages really drove a model call.

The test never spawns the real ``claude`` binary: the shim is found by name on
``PATH`` and ``setUp`` asserts that the resolved path is the shim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner import queue

APIARY_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# The fake `claude` CLI
# --------------------------------------------------------------------------- #

_FAKE_CLAUDE_SRC = r'''#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by runner/test_e2e_pipeline.py.

Reads the prompt on stdin, matches it against the opening line each runner
stage writes, and prints a Claude-CLI-shaped JSON envelope on stdout. For
executor steps it performs the file writes the real model would have made
(cwd is the worktree / target checkout, exactly as the stage set it).
"""
import json
import os
import re
import sys
from pathlib import Path


def envelope(text, turns=2):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "num_turns": turns,
        "duration_ms": 12,
        "total_cost_usd": 0.0012,
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 30,
        },
    })


def log(stage, detail=None):
    path = os.environ.get("APIARY_FAKE_CLAUDE_LOG")
    if not path:
        return
    row = {"stage": stage, "cwd": os.getcwd(), "argv": sys.argv[1:]}
    if detail is not None:
        row["detail"] = detail
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def apply_writes(writes):
    """Write {relative path: content} into the current working directory."""
    written = []
    for rel, content in sorted(writes.items()):
        target = Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def main():
    prompt = sys.stdin.read()
    script_path = os.environ.get("APIARY_FAKE_CLAUDE_SCRIPT", "")
    script = json.loads(Path(script_path).read_text(encoding="utf-8"))

    if "You are an autonomous spec writer" in prompt:
        log("refine")
        print(envelope(json.dumps(script["spec"])))
        return 0

    if "You are an autonomous implementation planner" in prompt:
        log("plan")
        print(envelope(json.dumps(script["plan"])))
        return 0

    match = re.search(r"^You are implementing step (\d+) of a plan\.", prompt, re.M)
    if match:
        step = match.group(1)
        writes = script.get("writes", {}).get(step, {})
        written = apply_writes(writes)
        log("execute", {"step": step, "wrote": written})
        print(envelope("Implemented step " + step + "."))
        return 0

    match = re.search(r"^You are verifying step (\d+) of a plan\.", prompt, re.M)
    if match:
        step = match.group(1)
        log("verify", {"step": step})
        print(envelope(json.dumps(
            script.get("verify", {"passed": True, "explanation": "checked"})
        )))
        return 0

    if "adversarial code reviewer (Attacker)" in prompt:
        log("attacker")
        print(envelope(json.dumps(script["findings"])))
        return 0

    if "You are a code defender" in prompt:
        written = apply_writes(script.get("defender_writes", {}))
        log("defender", {"wrote": written})
        print(envelope(json.dumps({"responses": script["responses"]})))
        return 0

    if "code review triage agent" in prompt:
        log("triage")
        print(envelope(json.dumps({"reviews": script["reviews"]})))
        return 0

    log("unmatched", {"head": prompt[:200]})
    sys.stderr.write("fake claude: unrecognised prompt\n")
    return 3


if __name__ == "__main__":
    sys.exit(main())
'''


def install_fake_claude(bin_dir: Path) -> Path:
    """Write the fake CLI plus a ``claude`` shim into *bin_dir*.

    Returns the path the shim will be resolved to. On Windows the shim must be
    a ``.bat``: ``CreateProcess`` only ever appends ``.exe`` to a bare name, so
    an extensionless script is invisible there.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE_SRC, encoding="utf-8")
    if os.name == "nt":
        shim = bin_dir / "claude.bat"
        shim.write_text(
            '@echo off\r\n"' + sys.executable + '" "' + str(script) + '" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = bin_dir / "claude"
        shim.write_text(
            "#!/bin/sh\nexec " + sys.executable + ' "' + str(script) + '" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return shim


# --------------------------------------------------------------------------- #
# Canned scenario
# --------------------------------------------------------------------------- #

GREETER_SOURCE = '''"""Greeting helper for the demo app."""


def greet(name):
    cleaned = (name or "").strip()
    if not cleaned:
        return "Hello there!"
    return "Hello, " + cleaned + "!"
'''

SPEC = {
    "goal": {
        "problem": (
            "The demo app has no greeting helper, so every caller repeats its own greeting string."
        ),
        "solution": "Add a greeter module exposing greet(name).",
        "value": "Callers share one greeting helper instead of duplicating strings.",
    },
    "shape": {
        "components": [
            {
                "name": "greeter",
                "description": "Module exposing greet(name), returning a greeting string.",
            }
        ],
        "integration_point": "app/greeter.py, imported by app/main.py",
        "pattern": "Module-level function, matching app/main.py",
        "data_flow": "name argument -> greet() -> greeting string",
        "dependencies": "Python standard library only",
    },
    "behavior": {
        "input": "A name string passed to greet().",
        "processing": "Trim the name, then format it into a greeting sentence.",
        "output": "A greeting string.",
        "error_cases": [
            {
                "trigger": "name argument is empty",
                "behavior": "greet returns a generic greeting",
            }
        ],
        "edge_cases": [
            {
                "condition": "name argument has surrounding whitespace",
                "behavior": "greet trims the whitespace first",
            }
        ],
    },
    "boundaries": {
        "in_scope": ["adding the greeter module"],
        "out_of_scope": [
            {
                "item": "localisation of the greeting",
                "reason": "the demo app has no translation layer yet",
            }
        ],
        "must_not_break": ["app/main.py keeps returning 0"],
    },
    "acceptance_criteria": [
        "Given the name Ada, when greet is called, then it returns a greeting containing Ada",
        "Given an empty name argument, when greet is called, then it returns a generic greeting",
        "Given a name argument with surrounding whitespace, when greet is called, then it trims the whitespace",
    ],
    "files_examined": [
        {
            "path": "app/main.py",
            "sha": None,
            "summary": "Entry point; plain module-level function returning 0.",
        }
    ],
}

_STEP1_SPEC = (
    "Create app/greeter.py with a greet(name) function. "
    "Given the name Ada, it returns a greeting containing Ada. "
    "Given an empty name argument, it returns a generic greeting. "
    "Given a name argument with surrounding whitespace, it trims the whitespace "
    "before formatting."
)

PLAN_STEPS = [
    {
        "step_number": 1,
        "type": "create",
        "description": "Add the greeter module with greet(name)",
        "action": "create",
        "files": ["app/greeter.py"],
        "depends_on": [],
        "code_spec": _STEP1_SPEC,
        "post_conditions": [
            {"type": "file_exists", "file": "app/greeter.py"},
            {"type": "file_contains", "file": "app/greeter.py", "text": "def greet"},
        ],
    },
    {
        "step_number": 2,
        "type": "verify",
        "description": "Confirm greet is defined and returns a greeting",
        "action": "verify",
        "files": [],
        "depends_on": [1],
        "code_spec": "Read app/greeter.py and confirm greet(name) exists.",
    },
    {
        # A modify step on a file that exists ONLY in the target repo. Before
        # validate_plan took its root from the plan's target_repo, this failed
        # with "file not found" on all three planner attempts and no
        # multi-repo run could get past stage 3 (review runner Bug 2).
        "step_number": 3,
        "type": "modify",
        "description": "Wire the greeter into the entry point",
        "action": "modify",
        "files": ["app/main.py"],
        "depends_on": [1],
        "code_spec": "Import greet from app/greeter.py and call it from main().",
        "post_conditions": [
            {"type": "file_contains", "file": "app/main.py", "text": "greet"},
        ],
    },
]

MAIN_SOURCE = """from app.greeter import greet


def main():
    print(greet("Ada"))
    return 0
"""


def scenario() -> dict:
    return {
        "spec": SPEC,
        "plan": {"steps": PLAN_STEPS},
        "writes": {
            "1": {"app/greeter.py": GREETER_SOURCE},
            "3": {"app/main.py": MAIN_SOURCE},
        },
        "verify": {"passed": True, "explanation": "greet(name) is defined."},
        "findings": [
            {
                "category": "input",
                "description": "greet() coerces a non-string name via strip and would raise.",
                "severity": "low",
                "location": "app/greeter.py:4-8",
            }
        ],
        "responses": [
            {
                "finding_ref": "ATK-001",
                "action": "deferred",
                "description": "Callers are typed; a non-string name is out of scope here.",
            }
        ],
        "reviews": [
            {
                "id": "ATK-001",
                "decision": "accept",
                "rationale": "Style nit on an internal helper; not a real risk.",
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def git_ok(repo: Path, *args: str) -> str:
    result = git(repo, *args)
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )
    return result.stdout.strip()


def make_target_repo(path: Path, origin: Path) -> str:
    """Create a target repo on ``master`` wired to a bare *origin*.

    Returns the initial commit sha.
    """
    path.mkdir(parents=True, exist_ok=True)
    git_ok(path.parent, "init", "--bare", str(origin))
    git_ok(path.parent, "init", str(path))
    git_ok(path, "config", "user.name", "Runner E2E")
    git_ok(path, "config", "user.email", "runner-e2e@example.invalid")
    git_ok(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(
        ".apiary/\n.runner-worktrees/\n__pycache__/\n",
        encoding="utf-8",
    )
    (path / "README.md").write_text("# demo app\n", encoding="utf-8")
    (path / "app").mkdir()
    (path / "app" / "main.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    git_ok(path, "add", "-A")
    git_ok(path, "commit", "-m", "initial commit")
    git_ok(path, "branch", "-M", "master")
    git_ok(path, "remote", "add", "origin", str(origin))
    git_ok(path, "push", "-u", "origin", "master")
    return git_ok(path, "rev-parse", "HEAD")


def origin_refs(origin: Path) -> dict:
    out = git_ok(origin, "for-each-ref", "--format=%(refname) %(objectname)")
    refs = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, sha = line.split(" ", 1)
        refs[name] = sha.strip()
    return refs


def runner_branches(repo: Path) -> list[str]:
    out = git_ok(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/runner/")
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_subjects(repo: Path, ref: str, base: str) -> list[str]:
    out = git_ok(repo, "log", "--format=%s", f"{base}..{ref}")
    return [line.strip() for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Test case
# --------------------------------------------------------------------------- #


class E2EPipelineBase(unittest.TestCase):
    """Shared fixture: temp origin + target repo, temp state dir, fake claude."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="runner_e2e_")
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.origin = self.root / "origin.git"
        self.target = self.root / "target"
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.bin_dir = self.root / "bin"
        self.state.mkdir()
        self.home.mkdir()
        self.base_sha = make_target_repo(self.target, self.origin)

        self.script_path = self.root / "scenario.json"
        self.script_path.write_text(json.dumps(scenario(), indent=2), encoding="utf-8")
        self.call_log = self.root / "claude_calls.jsonl"
        self.shim = install_fake_claude(self.bin_dir)

        self.env = self._build_env()
        resolved = shutil.which("claude", path=self.env["PATH"])
        self.assertIsNotNone(resolved, "fake claude shim not resolvable on PATH")
        self.assertEqual(
            Path(resolved).parent.resolve(),
            self.bin_dir.resolve(),
            "the fake claude shim must shadow any real claude on PATH",
        )

    def _cleanup(self):
        # Worktrees hold open handles on Windows; drop them before rmtree.
        for branch in runner_branches(self.target):
            git(self.target, "worktree", "prune")
            git(self.target, "branch", "-D", branch)
        self._tmp.cleanup()

    def _build_env(self) -> dict:
        # Scrub inherited APIARY_*/CLAUDE_* so a sibling test module's
        # module-level env assignment (e.g. APIARY_RUNNER_TEST_ISOLATION in
        # test_run_detached.py) cannot leak into the run.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("APIARY_", "CLAUDE_"))}
        env["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        env["PYTHONPATH"] = str(APIARY_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["APIARY_TARGET_STATE_DIR"] = str(self.state)
        # Keep the budgeter's real usage log out of this: log_agent_cost.py
        # refuses to write to the production path under this flag, so cost
        # logging degrades to a warning instead of polluting real data.
        env["APIARY_BUDGETER_TEST_ISOLATION"] = "1"
        env["APIARY_FAKE_CLAUDE_SCRIPT"] = str(self.script_path)
        env["APIARY_FAKE_CLAUDE_LOG"] = str(self.call_log)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        return env

    # -- helpers ---------------------------------------------------------- #

    def run_runner(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "runner.run", *args],
            cwd=str(APIARY_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )

    def intake_payload(self, uuid: str) -> dict:
        return {
            "id": uuid,
            "title": "Add a greeter module",
            "problem": (
                "The demo app has no greeting helper, so callers repeat their own greeting strings."
            ),
            "description": (
                "Add app/greeter.py exposing greet(name) so the demo app has a "
                "single greeting helper."
            ),
            "scope": "app/greeter.py only",
            "context": "",
            "created_at": "2026-08-26T00:00:00+00:00",
            "target_repo": str(self.target),
        }

    def artifact(self, kind: str, uuid: str) -> Path:
        return self.state / "runner" / kind / f"{uuid}.json"

    def claude_stages(self) -> list[str]:
        if not self.call_log.exists():
            return []
        return [
            json.loads(line)["stage"]
            for line in self.call_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def assert_nothing_pushed(self):
        refs = origin_refs(self.origin)
        self.assertEqual(
            refs,
            {"refs/heads/master": self.base_sha},
            "the runner must never push: origin picked up new or moved refs",
        )

    def assert_pipeline_artifacts(self, uuid: str):
        for kind in ("intake", "specs", "plans", "executions", "hardens", "reports"):
            path = self.artifact(kind, uuid)
            self.assertTrue(path.exists(), f"missing {kind} artifact at {path}")
        execution = json.loads(self.artifact("executions", uuid).read_text(encoding="utf-8"))
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(
            [step["status"] for step in execution["steps"]],
            ["passed"] * len(PLAN_STEPS),
        )
        report = json.loads(self.artifact("reports", uuid).read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], "all_resolved")
        return execution, report


class InteractiveRunTest(E2EPipelineBase):
    """`python -m runner.run <intake>` against the operator's checkout."""

    def test_full_pipeline_interactive(self):
        uuid = "e2e-interactive-0001"
        intake_path = self.state / "runner" / "intake" / f"{uuid}.json"
        intake_path.parent.mkdir(parents=True, exist_ok=True)
        intake_path.write_text(json.dumps(self.intake_payload(uuid), indent=2), encoding="utf-8")

        result = self.run_runner(str(intake_path), "--target-repo", str(self.target))
        self.assertEqual(
            result.returncode,
            0,
            f"runner failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

        # Every LLM stage really called the (fake) model.
        stages = self.claude_stages()
        self.assertEqual(
            [s for s in stages if s != "unmatched"],
            stages,
            f"fake claude saw an unrecognised prompt: {stages}",
        )
        for expected in ("refine", "plan", "execute", "verify", "attacker", "defender", "triage"):
            self.assertIn(expected, stages, f"stage {expected} never ran")

        execution, report = self.assert_pipeline_artifacts(uuid)
        self.assertEqual(report["path_taken"], "merged-locally")

        # The executor's work is a real commit, and approval squash-merged it
        # into the target repo's master locally.
        self.assertIn(
            "app/greeter.py",
            git_ok(self.target, "ls-tree", "--name-only", "-r", "master"),
        )
        subjects = commit_subjects(self.target, "master", self.base_sha)
        self.assertTrue(
            any(s.startswith(f"runner/{uuid}:") for s in subjects),
            f"no squash-merge commit on master: {subjects}",
        )
        self.assert_nothing_pushed()


class DetachedRunTest(E2EPipelineBase):
    """`python -m runner.run --detached` picking a backlog item."""

    def test_full_pipeline_detached(self):
        uuid = "e2e-detached-0001"
        backlog = self.state / "runner" / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        (backlog / "add-a-greeter-module.json").write_text(
            json.dumps(self.intake_payload(uuid), indent=2), encoding="utf-8"
        )

        result = self.run_runner("--detached")
        self.assertEqual(
            result.returncode,
            0,
            f"detached runner failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

        stages = self.claude_stages()
        for expected in ("refine", "plan", "execute", "verify", "attacker", "defender", "triage"):
            self.assertIn(expected, stages, f"stage {expected} never ran")

        execution, report = self.assert_pipeline_artifacts(uuid)
        # Detached runs stop at the branch: approval must never merge from a
        # worktree, so the branch is left for review.
        self.assertEqual(report["path_taken"], "worktree-deferred")

        # The backlog item was claimed and promoted, not left to be re-picked.
        self.assertEqual(list(backlog.glob("*.json")), [])

        # -- branch shape -------------------------------------------------- #
        # One branch per run (review runner Bug 3): the executor works on the
        # branch the worktree is already on instead of creating a second
        # `runner/<uuid>` beside it, so the execution log, run_history,
        # queue.py and max_unreviewed all name the same branch.
        branches = runner_branches(self.target)
        run_branch = f"runner/add-a-greeter-module-{uuid}"
        self.assertEqual(
            branches,
            [run_branch],
            "a run must leave exactly one runner/* branch behind",
        )
        self.assertEqual(execution["branch"], run_branch)
        subjects = commit_subjects(self.target, run_branch, "master")
        self.assertTrue(
            any(s.startswith(f"runner/{uuid} step 1:") for s in subjects),
            f"the executor's step commit is not on the run branch: {subjects}",
        )
        self.assertTrue(
            any(s.startswith(f"runner/{uuid} step 3:") for s in subjects),
            f"the modify step's commit is missing: {subjects}",
        )
        self.assertIn(f"runner/{uuid}: Add a greeter module", subjects)
        self.assertIn(
            "app/greeter.py",
            git_ok(self.target, "ls-tree", "--name-only", "-r", run_branch),
        )
        # The modify step touched a file that exists only in the target repo.
        self.assertIn(
            "from app.greeter import greet",
            git_ok(self.target, "show", f"{run_branch}:app/main.py"),
        )

        # The morning review table joins run_history to the live branch.
        row_uuid = queue.uuid_for_branch(run_branch, {uuid: {"uuid": uuid}})
        self.assertEqual(row_uuid, uuid)

        # The operator's checkout was never touched.
        self.assertEqual(git_ok(self.target, "rev-parse", "--abbrev-ref", "HEAD"), "master")
        self.assertEqual(git_ok(self.target, "rev-parse", "master"), self.base_sha)
        self.assertEqual(git_ok(self.target, "status", "--porcelain"), "")

        # The worktree is torn down on success.
        self.assertEqual(
            [],
            [
                line
                for line in git_ok(self.target, "worktree", "list", "--porcelain").splitlines()
                if "runner-worktrees" in line or ".runner-worktrees" in line
            ],
        )

        # -- run history --------------------------------------------------- #
        history_path = self.state / "runner" / "run_history.jsonl"
        self.assertTrue(history_path.exists(), "no run_history.jsonl was written")
        rows = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["exit_status"], "ok")
        self.assertEqual(row["uuid"], uuid)
        self.assertEqual(row["stages_completed"], 6)
        self.assertEqual(row["branch"], run_branch)
        self.assertEqual(Path(row["target_repo"]).resolve(), self.target.resolve())
        self.assertGreater(row["total_tokens"], 0)

        # No lock and no tracker survive a clean run.
        self.assertEqual(list((self.state / "runner" / "locks").glob("*.lock")), [])
        runs_dir = self.state / "runner" / "runs"
        self.assertEqual(list(runs_dir.glob("*.json")) if runs_dir.exists() else [], [])

        self.assert_nothing_pushed()


if __name__ == "__main__":
    unittest.main()
