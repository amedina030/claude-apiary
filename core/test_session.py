"""Where session state lives (review S1): identity + history under the
target's state dir, hook flags under the repo's session-tmp, never ~/.claude."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import session as sess
from core.session import SessionId, load_identity

SID = "abcd1234-1111-2222-3333-444444444444"


class _Env(unittest.TestCase):
    VARS = (
        "CLAUDE_PROJECT_DIR",
        "APIARY_TARGET_REPO",
        "APIARY_TARGET_STATE_DIR",
        "HOME",
        "USERPROFILE",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / ".claude" / "apiary" / "session-tmp").mkdir(parents=True)
        (self.repo / ".claude" / "apiary" / "self-pointer.json").write_text(
            json.dumps(
                {"schema_version": 1, "name": "repo", "uid": 1, "real_path": str(self.repo)}
            ),
            encoding="utf-8",
        )
        self.state = self.root / "state"
        self.state.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self._saved = {v: os.environ.get(v) for v in self.VARS}
        for v in self.VARS:
            os.environ.pop(v, None)
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        os.environ["APIARY_TARGET_STATE_DIR"] = str(self.state)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for v, val in self._saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val


class PathsTest(_Env):
    def test_identity_under_state_sessions(self):
        self.assertEqual(
            SessionId(SID).identity_path(), self.state / "sessions" / "identity-abcd1234.json"
        )

    def test_flags_under_repo_session_tmp(self):
        self.assertEqual(
            SessionId(SID).flag_path("startup_done"),
            self.repo / ".claude" / "apiary" / "session-tmp" / f"{SID}_startup_done",
        )

    def test_explicit_base_wins(self):
        base = self.root / "elsewhere"
        self.assertEqual(SessionId(SID).identity_path(base), base / "identity-abcd1234.json")
        self.assertEqual(SessionId(SID).flag_path("x", base), base / f"{SID}_x")

    def test_fallback_is_temp_not_home(self):
        os.environ.pop("APIARY_TARGET_REPO")
        os.environ.pop("APIARY_TARGET_STATE_DIR")
        # Force the git-root probe to fail by leaving the checkout.
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            tmp = sess.session_tmp_dir()
            sessions = sess.sessions_dir()
        finally:
            os.chdir(cwd)
        for p in (tmp, sessions):
            self.assertNotIn(str(self.home), str(p))
            self.assertNotIn(".claude", p.parts)
            self.assertTrue(str(p).startswith(tempfile.gettempdir()))


class LoadIdentityTest(_Env):
    def _write(self, sid, role, mtime=None):
        p = SessionId(sid).identity_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": role, "mission": "general"}), encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_specific_session(self):
        self._write(SID, "reviewer")
        ident = load_identity(SID)
        self.assertEqual((ident["role"], ident["session_id"]), ("reviewer", "abcd1234"))

    def test_missing_gives_defaults(self):
        self.assertEqual(load_identity(SID)["role"], "user")
        self.assertEqual(load_identity()["session_id"], "")

    def test_most_recent_when_unspecified(self):
        self._write("11111111-1111-2222-3333-444444444444", "old", mtime=1_000_000)
        self._write("22222222-1111-2222-3333-444444444444", "new", mtime=2_000_000)
        ident = load_identity()
        self.assertEqual((ident["role"], ident["session_id"]), ("new", "22222222"))

    def test_nothing_under_home(self):
        self._write(SID, "reviewer")
        load_identity()
        self.assertFalse((self.home / ".claude").exists())


class HistoryShapeTest(unittest.TestCase):
    def test_load_accepts_v1_dict_and_bare_list_and_junk(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "history.json"
            p.write_text(
                json.dumps({"schema_version": 1, "sessions": [{"session_id": "a"}, 3]}),
                encoding="utf-8",
            )
            self.assertEqual(sess.load_history(p), [{"session_id": "a"}])
            p.write_text(json.dumps([{"session_id": "b"}]), encoding="utf-8")
            self.assertEqual(sess.load_history(p), [{"session_id": "b"}])
            p.write_text("not json", encoding="utf-8")
            self.assertEqual(sess.load_history(p), [])
            self.assertEqual(sess.load_history(Path(td) / "missing.json"), [])
            self.assertEqual(
                json.loads(sess.dump_history([{"x": 1}])),
                {"schema_version": 1, "sessions": [{"x": 1}]},
            )


class FindStateDirTest(unittest.TestCase):
    def _pinned(self, root):
        from core.utils import state

        main = root / "apiary"
        (main / ".repos" / "proj-7").mkdir(parents=True)
        repo = root / "proj"
        (repo / ".claude" / "apiary").mkdir(parents=True, exist_ok=True)
        state.write_main_apiary_pointer(
            repo, {"main_apiary_path": str(main), "main_apiary_uid": 1, "schema_version": 1}
        )
        state.write_self_pointer(
            repo, {"name": "proj", "uid": 7, "real_path": str(repo), "schema_version": 1}
        )
        return main, repo

    def test_live_pins_resolve_and_legacy_pointer_still_works(self):
        from core.utils import state

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "proj"
            (repo / ".claude" / "apiary").mkdir(parents=True)
            self.assertIsNone(state.find_state_dir(repo))  # no pins yet
            main, repo = self._pinned(root)
            self.assertEqual(state.find_state_dir(repo), main / ".repos" / "proj-7")
            # Pins pointing at a dir that does not exist -> None, not a guess.
            state.write_self_pointer(
                repo, {"name": "proj", "uid": 8, "real_path": str(repo), "schema_version": 1}
            )
            self.assertIsNone(state.find_state_dir(repo))
            # Legacy breadcrumb model.
            legacy = root / "old"
            (legacy / ".apiary").mkdir(parents=True)
            (main / ".repos" / "old-3").mkdir()
            (legacy / ".apiary" / "pointer").write_text(
                json.dumps({"apiary_repo": str(main), "target_id": "old-3"}), encoding="utf-8"
            )
            self.assertEqual(state.find_state_dir(legacy), main / ".repos" / "old-3")

    def test_session_dirs_follow_the_pins_without_launcher_env(self):
        with tempfile.TemporaryDirectory() as td:
            main, repo = self._pinned(Path(td))
            saved = {
                v: os.environ.get(v)
                for v in ("APIARY_TARGET_STATE_DIR", "CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO")
            }
            try:
                os.environ.pop("APIARY_TARGET_STATE_DIR", None)
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                os.environ["APIARY_TARGET_REPO"] = str(repo)
                self.assertEqual(sess.sessions_dir(), main / ".repos" / "proj-7" / "sessions")
            finally:
                for v, val in saved.items():
                    if val is None:
                        os.environ.pop(v, None)
                    else:
                        os.environ[v] = val


if __name__ == "__main__":
    unittest.main()
