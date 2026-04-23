#!/usr/bin/env python3
"""Tests for core.startup / core.hooks.startup_prompt_hook under the repo
state layout (APIARY_STATE_LAYOUT=repo) introduced by todo #264.

Covers two layers:
  1. core.startup.run_summary reads notes from <session-repo>/.apiary/scribe/
     when the env var is set and the session's cwd is inside a git repo.
  2. core.hooks.startup_prompt_hook._run() skips summary/learnings injection
     when the session's cwd is not inside a git repo (fallback behavior).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import startup  # noqa: E402
import scribe.notes as notes  # noqa: E402


def _write_note(state_dir: Path, *, note_id: int, content: str, ntype: str = "todo"):
    """Seed a note into the scribe store so run_summary picks it up.

    note_id is retained for API compatibility but ignored — the store assigns
    (year, seq) sequentially.
    """
    from scribe.store import ScribeStore
    store = ScribeStore(state_dir)
    store.add_note(ntype, content, "test")


class RunSummaryRepoLayoutTests(unittest.TestCase):
    """run_summary reads from <session-repo>/.apiary/scribe/ in repo layout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()
        self.fake_repo = self.tmp_path / "fake-session-repo"
        self.fake_repo.mkdir()
        self.state_dir = self.fake_repo / ".apiary" / "scribe"

        self._env_patch = mock.patch.dict(
            os.environ, {notes.STATE_LAYOUT_ENV: "repo"}
        )
        self._env_patch.start()
        # Force _git_repo_root to resolve the fake repo regardless of cwd.
        self._git_patch = mock.patch.object(
            notes, "_git_repo_root", return_value=self.fake_repo
        )
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_reads_notes_from_repo_state_dir(self):
        _write_note(
            self.state_dir, note_id=1, content="repo-layout note", ntype="todo"
        )
        output = startup.run_summary(str(self.fake_repo), "user", "general")
        self.assertIn("repo-layout note", output)

    def test_empty_state_dir_yields_zero_notes(self):
        output = startup.run_summary(str(self.fake_repo), "user", "general")
        self.assertIn("Active items:", output)
        self.assertIn("0 notes", output)

    def test_legacy_state_is_not_consulted_in_repo_layout(self):
        # Write a note into the legacy project-key location and verify it
        # does NOT appear in the repo-layout summary.
        legacy_key = "synthetic-legacy-key-for-test"
        legacy_dir = notes.PROJECTS_DIR / legacy_key
        try:
            _write_note(
                legacy_dir, note_id=1, content="legacy-only note", ntype="todo"
            )
            with mock.patch.object(
                startup, "get_project_key", return_value=legacy_key
            ):
                output = startup.run_summary(
                    str(self.fake_repo), "user", "general"
                )
            self.assertNotIn("legacy-only note", output)
        finally:
            # Clean up the synthetic legacy dir we created in the user's
            # real ~/.claude/projects/ tree.
            for name in ("notes.jsonl", "notes_archive.jsonl", "learnings.jsonl"):
                p = legacy_dir / name
                if p.exists():
                    p.unlink()
            if legacy_dir.exists() and not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()


class StartupPromptHookSkipTests(unittest.TestCase):
    """startup_prompt_hook._run() skips notes injection when repo layout is
    on and the session's cwd is not inside a git repo.

    Exercises the hook in-process by stubbing the payload reader and all
    downstream effects so the test stays hermetic.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()
        self.non_repo_cwd = self.tmp_path / "not-a-repo"
        self.non_repo_cwd.mkdir()

        # Import the hook lazily so patches land before any caching.
        import core.hooks.startup_prompt_hook as hook
        self.hook = hook

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_payload(self, session_id: str):
        return {
            "session_id": session_id,
            "message": "",
            "cwd": str(self.non_repo_cwd),
        }

    def test_skip_branch_fires_when_not_in_git_repo(self):
        session_id = "a" * 36  # 36-char UUID-ish placeholder
        captured = {}

        def fake_hook_allow(*args, event=None, **kwargs):
            captured["args"] = args
            captured["event"] = event

        fake_sid = mock.MagicMock()
        fake_sid.full = session_id
        fake_sid.flag_path.return_value = self.tmp_path / "flag"

        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "repo"}), \
                mock.patch.object(
                    self.hook, "read_payload",
                    return_value=self._fake_payload(session_id),
                ), \
                mock.patch.object(self.hook, "SessionId", return_value=fake_sid), \
                mock.patch.object(
                    self.hook, "run_init",
                    return_value={"identity": {"role": "user", "mission": "general"}},
                ), \
                mock.patch.object(self.hook, "run_summary") as mock_summary, \
                mock.patch("subprocess.run") as mock_subproc, \
                mock.patch.object(self.hook, "_git_repo_root", return_value=None), \
                mock.patch.object(self.hook, "hook_allow", side_effect=fake_hook_allow):
            self.hook._run()

        # run_summary must not be called at all in the skip branch.
        mock_summary.assert_not_called()
        # The learnings subprocess must also be skipped.
        mock_subproc.assert_not_called()
        # And the emitted context must contain the skip marker so operators
        # can tell the skip fired.
        self.assertTrue(captured)
        emitted = captured["args"][0] if captured["args"] else ""
        self.assertIn("summary: skipped", emitted)

    def test_repo_layout_in_git_repo_invokes_summary(self):
        """Positive counterpart: when cwd IS in a git repo, run_summary fires."""
        session_id = "b" * 36
        captured = {}

        def fake_hook_allow(*args, event=None, **kwargs):
            captured["args"] = args

        fake_sid = mock.MagicMock()
        fake_sid.full = session_id
        fake_sid.flag_path.return_value = self.tmp_path / "flag2"

        fake_repo = self.tmp_path / "fake-repo"
        fake_repo.mkdir()

        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "repo"}), \
                mock.patch.object(
                    self.hook, "read_payload",
                    return_value=self._fake_payload(session_id),
                ), \
                mock.patch.object(self.hook, "SessionId", return_value=fake_sid), \
                mock.patch.object(
                    self.hook, "run_init",
                    return_value={"identity": {"role": "user", "mission": "general"}},
                ), \
                mock.patch.object(
                    self.hook, "run_summary", return_value="real summary text"
                ) as mock_summary, \
                mock.patch("subprocess.run") as mock_subproc, \
                mock.patch.object(
                    self.hook, "_git_repo_root", return_value=fake_repo
                ), \
                mock.patch.object(self.hook, "hook_allow", side_effect=fake_hook_allow):
            mock_subproc.return_value = mock.MagicMock(returncode=0, stdout="")
            self.hook._run()

        mock_summary.assert_called_once()
        # The learnings subprocess should be spawned with repo-root cwd and
        # APIARY_STATE_LAYOUT=repo in its env.
        self.assertEqual(mock_subproc.call_count, 1)
        kwargs = mock_subproc.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(fake_repo))
        self.assertEqual(kwargs["env"].get(notes.STATE_LAYOUT_ENV), "repo")


if __name__ == "__main__":
    unittest.main()
