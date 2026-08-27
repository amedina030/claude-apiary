"""Tests for App's session list: open/switch/close bookkeeping, the single
replay path, and pending-permission routing.

`App` owns the tab list, the active-tab pointer and the pending-permission
maps, and every one of those is mutated from a different thread (each
`pywebview.api.*` call gets its own, the permission bridge's HTTP handler
gets another). It had no tests at all, and that is exactly where review
bugs #5 and #6 live — index arithmetic that a concurrent close invalidates,
and unlocked permission bookkeeping.

Sessions are faked: a real one spawns a pty, three poller threads and a
scribe aggregator. The fake records the lifecycle calls App is contracted to
make (`start`, `stop`, `flush_notes`, `file_refs.destroy`) so ordering
regressions are visible.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gui import app as gui_app
from gui.app import App


class FakeFileRefs:
    def __init__(self) -> None:
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1

    def list(self) -> list[dict]:
        return []


class FakeTracker:
    def __init__(self, agents=None) -> None:
        self._agents = agents or []

    def snapshot(self):
        return list(self._agents)


class FakeSession:
    """Stand-in for gui.session.Session with the surface App touches."""

    def __init__(self, cwd: Path, accept_edits: bool = False, start_ok: bool = True):
        self.session_id = str(uuid.uuid4())
        self.cwd = Path(cwd)
        self.accept_edits = accept_edits
        self.file_refs = FakeFileRefs()
        self.current_path: Path | None = None
        self.subagent_tracker = None
        self._start_ok = start_ok
        self.started = 0
        self.stopped = 0
        self.notes_flushed = 0

    def start(self) -> bool:
        self.started += 1
        return self._start_ok

    def stop(self) -> None:
        self.stopped += 1

    def flush_notes(self) -> None:
        self.notes_flushed += 1


class FakeWindow:
    """Captures the JS App would have evaluated, so pushes are assertable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate_js(self, js: str) -> None:
        self.calls.append(js)


class FakeBridge:
    """Minimal PermissionBridge: records resolves, reports what's pending."""

    def __init__(self, pending=()):
        self._pending = list(pending)
        self.resolved: list[tuple[str, dict]] = []

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def resolve(self, pending_id: str, decision: dict) -> bool:
        self.resolved.append((pending_id, decision))
        if pending_id in self._pending:
            self._pending.remove(pending_id)
            return True
        return False


class AppSessionsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # FileRefs.wipe_all touches the real per-profile state dir; tabs_state
        # .save writes tabs.json there. Neither belongs in a unit test.
        for target, attr in (
            (gui_app.file_refs.FileRefs, "wipe_all"),
            (gui_app.tabs_state, "save"),
        ):
            patcher = mock.patch.object(target, attr)
            setattr(self, f"_{attr}_mock", patcher.start())
            self.addCleanup(patcher.stop)
        self.app = App()
        self.window = FakeWindow()
        self.app.window = self.window
        self.created: list[FakeSession] = []
        self.start_ok = True

    def _fake_create(self, cwd, accept_edits: bool = False) -> FakeSession:
        sess = FakeSession(cwd, accept_edits=accept_edits, start_ok=self.start_ok)
        self.created.append(sess)
        return sess

    def _open(self, name: str) -> FakeSession:
        """Open a tab rooted at a real (temp) directory and return its session."""
        d = self.root / name
        d.mkdir(exist_ok=True)
        with mock.patch.object(self.app, "_create_session", self._fake_create):
            sid = self.app.open_session(str(d))
        self.assertIsNotNone(sid, f"open_session({name}) failed")
        return self.created[-1]

    def _ids(self) -> list[str]:
        return [s.session_id for s in self.app._sessions]

    def _descriptor(self) -> list[dict]:
        with self.app._state_lock:
            return self.app._sessions_descriptor()

    def _js(self, needle: str) -> list[str]:
        return [c for c in self.window.calls if needle in c]


class OpenSessionTest(AppSessionsTestBase):
    def test_open_appends_and_activates(self):
        a = self._open("a")
        self.assertIs(self.app.active, a)
        b = self._open("b")
        self.assertIs(self.app.active, b, "a newly opened tab becomes active")
        self.assertEqual(self._ids(), [a.session_id, b.session_id])
        self.assertEqual([s.started for s in (a, b)], [1, 1])

    def test_failed_start_is_not_added(self):
        a = self._open("a")
        self.start_ok = False
        d = self.root / "dead"
        d.mkdir()
        with mock.patch.object(self.app, "_create_session", self._fake_create):
            self.assertIsNone(self.app.open_session(str(d)))
        self.assertEqual(self._ids(), [a.session_id])
        self.assertIs(self.app.active, a, "a failed spawn must not steal focus")

    def test_non_directory_is_rejected_with_a_toast(self):
        with mock.patch.object(self.app, "_create_session", self._fake_create):
            self.assertIsNone(self.app.open_session(str(self.root / "nope")))
        self.assertEqual(self.created, [])
        self.assertTrue(self._js("onToast"))

    def test_descriptor_marks_exactly_one_tab_active(self):
        self._open("a")
        b = self._open("b")
        rows = self._descriptor()
        self.assertEqual([r["active"] for r in rows], [False, True])
        self.assertEqual(rows[1]["session_id"], b.session_id)
        self.assertEqual(rows[0]["label"], "a")

    def test_persisted_active_index_is_derived_from_the_active_id(self):
        self._open("a")
        self._open("b")
        entries, idx = self._save_mock.call_args[0]
        self.assertEqual([e.cwd.name for e in entries], ["a", "b"])
        self.assertEqual(idx, 1)


class SwitchSessionTest(AppSessionsTestBase):
    def test_switch_activates_and_replays(self):
        a = self._open("a")
        b = self._open("b")
        self.window.calls.clear()
        self.assertTrue(self.app.switch_to(a.session_id))
        self.assertIs(self.app.active, a)
        self.assertTrue(self._js("onClear"), "switching replays the incoming tab")
        self.assertEqual(a.notes_flushed, 2, "one on open, one on switch-in")
        self.assertEqual(b.notes_flushed, 1)

    def test_switch_to_the_active_tab_is_a_no_op(self):
        a = self._open("a")
        self.window.calls.clear()
        self.assertTrue(self.app.switch_to(a.session_id))
        self.assertEqual(self.window.calls, [], "no re-replay of the current tab")

    def test_switch_to_unknown_id_returns_false(self):
        a = self._open("a")
        self.assertFalse(self.app.switch_to("nope"))
        self.assertFalse(self.app.switch_to(""))
        self.assertIs(self.app.active, a)


class CloseSessionTest(AppSessionsTestBase):
    def test_closing_a_non_active_tab_keeps_the_same_tab_active(self):
        # The index-arithmetic bug: with `_active_idx` this had to be fixed up
        # by hand, and got it wrong when two closes interleaved.
        a = self._open("a")
        b = self._open("b")
        c = self._open("c")
        self.assertIs(self.app.active, c)
        self.assertTrue(self.app.close_session(a.session_id))
        self.assertEqual(self._ids(), [b.session_id, c.session_id])
        self.assertIs(self.app.active, c, "closing an earlier tab must not move focus")

    def test_closing_the_active_tab_promotes_the_one_that_slides_in(self):
        a = self._open("a")
        b = self._open("b")
        c = self._open("c")
        self.app.switch_to(b.session_id)
        self.assertTrue(self.app.close_session(b.session_id))
        self.assertIs(self.app.active, c, "the next tab takes the closed slot")
        self.assertEqual(self._ids(), [a.session_id, c.session_id])

    def test_closing_the_active_last_tab_promotes_its_left_neighbour(self):
        a = self._open("a")
        b = self._open("b")
        self.assertTrue(self.app.close_session(b.session_id))
        self.assertIs(self.app.active, a)

    def test_closing_the_only_tab_leaves_no_active(self):
        a = self._open("a")
        self.assertTrue(self.app.close_session(a.session_id))
        self.assertEqual(self._ids(), [])
        self.assertIsNone(self.app.active)
        self.assertEqual(self.app._active_id, "")
        # Empty state still gets pushed so the frontend shows the picker CTA.
        self.assertTrue(self._js("setActiveSession"))

    def test_close_tears_the_session_down_once(self):
        a = self._open("a")
        self.assertTrue(self.app.close_session(a.session_id))
        self.assertEqual(a.stopped, 1)
        self.assertEqual(a.file_refs.destroyed, 1)

    def test_double_close_is_a_no_op_the_second_time(self):
        a = self._open("a")
        b = self._open("b")
        self.assertTrue(self.app.close_session(a.session_id))
        self.assertFalse(self.app.close_session(a.session_id))
        self.assertEqual(a.stopped, 1, "teardown must not run twice")
        self.assertEqual(self._ids(), [b.session_id])
        self.assertIs(self.app.active, b)

    def test_concurrent_closes_of_one_tab_produce_exactly_one_winner(self):
        # A double-click on the × fans out to two bridge threads. Exactly one
        # may pop the session; the loser must not corrupt the list.
        a = self._open("a")
        b = self._open("b")
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def close():
            barrier.wait()
            ok = self.app.close_session(a.session_id)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=close) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(results.count(True), 1, f"expected one winner, got {results}")
        self.assertEqual(self._ids(), [b.session_id])
        self.assertEqual(a.stopped, 1)

    def test_concurrent_opens_and_closes_keep_the_list_consistent(self):
        keep = self._open("keep")
        sessions = [self._open(f"s{i}") for i in range(8)]
        self.app.switch_to(keep.session_id)
        threads = [
            threading.Thread(target=self.app.close_session, args=(s.session_id,))
            for s in sessions
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(self._ids(), [keep.session_id])
        self.assertIs(self.app.active, keep)
        self.assertTrue(all(s.stopped == 1 for s in sessions))


class ReplayActiveTest(AppSessionsTestBase):
    def _transcript(self, name: str) -> Path:
        p = self.root / name
        p.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "u1",
                    "timestamp": "2026-08-26T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-x",
                        "content": [{"type": "text", "text": "hello"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return p

    def test_replay_pushes_clear_then_history_then_notes(self):
        a = self._open("a")
        a.current_path = self._transcript("a.jsonl")
        self.window.calls.clear()
        self.app._replay_active()
        self.assertTrue(self._js("onClear"))
        pushed = self._js("onMessages")
        self.assertEqual(len(pushed), 1)
        self.assertIn("hello", pushed[0])
        self.assertIn(a.session_id, pushed[0], "history is tagged with its tab")
        self.assertEqual(a.notes_flushed, 2)

    def test_replay_survives_a_missing_transcript(self):
        a = self._open("a")
        a.current_path = self.root / "gone.jsonl"
        self.window.calls.clear()
        self.app._replay_active()
        self.assertTrue(self._js("onStatus"), "the read error is surfaced")
        self.assertFalse(self._js("onMessages"))
        self.assertEqual(a.notes_flushed, 2, "notes still refresh after a bad read")

    def test_replay_pushes_the_agent_snapshot(self):
        a = self._open("a")
        a.subagent_tracker = FakeTracker([])
        self.window.calls.clear()
        self.app._replay_active()
        self.assertTrue(self._js("onAgents"))

    def test_replay_with_no_active_tab_does_nothing(self):
        self.window.calls.clear()
        self.app._replay_active()
        self.assertEqual(self.window.calls, [])

    def test_resync_after_reload_replays_the_active_tab(self):
        a = self._open("a")
        a.current_path = self._transcript("a.jsonl")
        self.window.calls.clear()
        with mock.patch.object(gui_app, "load_theme", return_value=({}, None)):
            self.app._resync_frontend_state()
        self.assertTrue(self._js("onTheme"))
        self.assertTrue(self._js("onMessages"))


class PendingPermissionTest(AppSessionsTestBase):
    def _prompt(self, sess, pending_id="p1"):
        self.app._push_permission_prompt(
            pending_id, {"session_id": sess.session_id, "tool_name": "Bash"}
        )

    def test_prompt_badges_its_own_tab_only(self):
        a = self._open("a")
        b = self._open("b")
        self._prompt(a)
        rows = {r["session_id"]: r["pending_permission"] for r in self._descriptor()}
        self.assertTrue(rows[a.session_id])
        self.assertFalse(rows[b.session_id])
        self.assertTrue(self._js("onPermissionPrompt"))

    def test_prompt_without_a_session_id_still_reaches_the_banner(self):
        self._open("a")
        self.app._push_permission_prompt("p1", {"tool_name": "Bash"})
        self.assertTrue(self._js("onPermissionPrompt"))
        self.assertEqual(self.app._pending_permission_by_session, {})

    def test_switching_to_a_badged_tab_re_surfaces_the_banner(self):
        a = self._open("a")
        self._open("b")
        self._prompt(a)
        self.window.calls.clear()
        self.app.switch_to(a.session_id)
        self.assertTrue(
            self._js("onPermissionPrompt"),
            "a prompt raised while the tab was in the background must re-banner",
        )

    def test_resolve_clears_the_badge_and_the_reverse_index(self):
        a = self._open("a")
        self.app._permission_bridge = FakeBridge(pending=["p1"])
        self._prompt(a)
        self.assertTrue(self.app.resolve_permission("p1", "allow", updated_input={"x": 1}))
        self.assertEqual(self.app._pending_permission_by_session, {})
        self.assertEqual(self.app._session_by_pending_id, {})
        pending_id, decision = self.app._permission_bridge.resolved[-1]
        self.assertEqual(pending_id, "p1")
        self.assertEqual(decision, {"behavior": "allow", "updatedInput": {"x": 1}})

    def test_resolve_of_an_unknown_id_is_false_and_harmless(self):
        a = self._open("a")
        self.app._permission_bridge = FakeBridge(pending=["p1"])
        self._prompt(a)
        self.assertFalse(self.app.resolve_permission("stale", "deny", message="no"))
        self.assertIn(a.session_id, self.app._pending_permission_by_session)

    def test_resolve_without_a_bridge_is_false(self):
        a = self._open("a")
        self._prompt(a)
        self.assertFalse(self.app.resolve_permission("p1", "deny"))

    def test_closing_a_badged_tab_denies_its_prompt(self):
        a = self._open("a")
        self.app._permission_bridge = FakeBridge(pending=["p1"])
        self._prompt(a)
        self.assertTrue(self.app.close_session(a.session_id))
        self.assertEqual(
            self.app._permission_bridge.resolved,
            [("p1", {"behavior": "deny", "message": "tab closed"})],
            "a closed tab must unblock its MCP caller, not wait out the timeout",
        )
        self.assertEqual(self.app._pending_permission_by_session, {})
        self.assertEqual(self.app._session_by_pending_id, {})

    def test_sweep_drops_prompts_the_bridge_no_longer_holds(self):
        a = self._open("a")
        self.app._permission_bridge = FakeBridge(pending=["p1"])
        self._prompt(a)
        # The bridge timed out internally (5-min deny) without telling us.
        self.app._permission_bridge._pending.clear()
        self.app._push_sessions()
        self.assertEqual(self.app._pending_permission_by_session, {})
        self.assertEqual(self.app._session_by_pending_id, {})

    def test_shutdown_clears_sessions_and_pending_state(self):
        a = self._open("a")
        self.app._permission_bridge = FakeBridge(pending=["p1"])
        self._prompt(a)
        self.app.shutdown()
        self.assertEqual(self._ids(), [])
        self.assertIsNone(self.app.active)
        self.assertEqual(a.stopped, 1)
        self.assertEqual(self.app._pending_permission_by_session, {})


if __name__ == "__main__":
    unittest.main()
