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
    VARS = ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO", "APIARY_TARGET_STATE_DIR", "HOME", "USERPROFILE")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / ".claude" / "apiary" / "session-tmp").mkdir(parents=True)
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
        self.assertEqual(SessionId(SID).identity_path(), self.state / "sessions" / "identity-abcd1234.json")

    def test_flags_under_repo_session_tmp(self):
        self.assertEqual(SessionId(SID).flag_path("startup_done"),
                         self.repo / ".claude" / "apiary" / "session-tmp" / f"{SID}_startup_done")

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


if __name__ == "__main__":
    unittest.main()
