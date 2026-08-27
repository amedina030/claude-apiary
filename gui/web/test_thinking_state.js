// Unit tests for thinking_state.js. Run with `node test_thinking_state.js`
// (no npm dependencies), or via `pytest gui/test_js_suites.py`.
//
// Time is a parameter everywhere in this module, so a whole turn — send,
// chunks, chained messages, end_turn, idle timeout — is exercised without a
// single sleep. The cross-check against bubble_monitor.js matters: the monitor
// classifies a missing bubble purely from `lastHideReason`, so if the two
// files drift on that vocabulary the anomaly log silently degrades to
// "unknown".

const assert = require("node:assert/strict");
const { test } = require("node:test");
const S = require("./thinking_state");
const { classifyBubbleAnomaly } = require("./bubble_monitor");

const T0 = 1_000_000;

function armedTab(now = T0) {
  const t = S.newTabState();
  S.startTurn(t, now);
  S.armTurn(t, now);
  return t;
}

// --- shape ----------------------------------------------------------------

test("a fresh tab state is idle, with empty drafts", () => {
  const t = S.newTabState();
  assert.equal(t.waitingForAssistant, false);
  assert.equal(t.thinkingStartTs, 0);
  assert.equal(t.lastPtyChunkAt, 0);
  assert.equal(t.lastAsstMsgAt, 0);
  assert.equal(t.shownThisTurn, false);
  assert.equal(t.lastHideReason, "");
  assert.equal(t.draft, "");
  assert.equal(t.captureDraft, "");
});

test("each tab gets its own state object", () => {
  const a = S.newTabState();
  const b = S.newTabState();
  S.armTurn(a, T0);
  assert.equal(b.waitingForAssistant, false, "a background tab's turn is its own");
});

// --- arming ---------------------------------------------------------------

test("startTurn resets the per-turn monitor fields without arming", () => {
  const t = S.newTabState();
  t.shownThisTurn = true;
  t.lastHideReason = "end_turn";
  S.startTurn(t, T0);
  assert.equal(t.thinkingStartTs, T0);
  assert.equal(t.shownThisTurn, false);
  assert.equal(t.lastHideReason, "");
  assert.equal(t.waitingForAssistant, false, "the composer arms separately");
});

test("armTurn is idempotent and never re-seeds the clock", () => {
  const t = S.newTabState();
  assert.equal(S.armTurn(t, T0), true);
  assert.equal(t.thinkingStartTs, T0);
  assert.equal(S.armTurn(t, T0 + 5000), false, "already armed");
  assert.equal(t.thinkingStartTs, T0, "a queued send must not restart the clock");
});

test("a user record arms a turn that did not start in the composer", () => {
  const t = S.newTabState();
  assert.equal(S.noteUserRecord(t, T0), true);
  assert.equal(t.waitingForAssistant, true);
  assert.equal(t.thinkingStartTs, T0);
  assert.equal(t.shownThisTurn, false);
});

test("a late user record for a turn in progress does not clobber it", () => {
  const t = armedTab();
  t.shownThisTurn = true;
  assert.equal(S.noteUserRecord(t, T0 + 3000), false);
  assert.equal(t.thinkingStartTs, T0, "the elapsed counter keeps counting");
  assert.equal(t.shownThisTurn, true, "monitor state survives");
});

// --- assistant records ----------------------------------------------------

test("end_turn ends the turn and flashes the taskbar", () => {
  const t = armedTab();
  const act = S.noteAssistantRecord(t, { stop_reason: "end_turn" }, T0 + 4000);
  assert.deepEqual(act.hide, { endTurn: true, reason: "end_turn" });
  assert.equal(act.flash, true);
  assert.equal(act.ensureBubble, false);
  assert.equal(t.lastAsstMsgAt, T0 + 4000);
  S.applyHide(t, act.hide.endTurn, act.hide.reason);
  assert.equal(t.waitingForAssistant, false);
  assert.equal(t.thinkingStartTs, 0);
  assert.equal(S.elapsedSeconds(t, T0 + 9000), null, "the badge hides");
});

test("a chained message hides transiently and keeps the turn alive", () => {
  const t = armedTab();
  const act = S.noteAssistantRecord(t, { stop_reason: null }, T0 + 2000);
  assert.deepEqual(act.hide, { endTurn: false, reason: "transient" });
  assert.equal(act.ensureBubble, true, "the bubble comes straight back");
  assert.equal(act.flash, false);
  S.applyHide(t, act.hide.endTurn, act.hide.reason);
  assert.equal(t.waitingForAssistant, true, "still mid-turn");
  assert.equal(t.thinkingStartTs, T0, "the elapsed counter is not restarted");
});

test("a chained message arms a turn nobody armed (the arming_gap case)", () => {
  // Terminal-pane typing / slash command / wakeup: no composer send ever
  // happened, so without this the bubble would never appear for the turn.
  const t = S.newTabState();
  const act = S.noteAssistantRecord(t, {}, T0);
  assert.equal(act.armed, true);
  assert.equal(t.waitingForAssistant, true);
  assert.equal(t.thinkingStartTs, T0);
  assert.equal(act.ensureBubble, true);
});

test("an assistant record on a background tab still records its timing", () => {
  const t = S.newTabState();
  S.noteAssistantRecord(t, { stop_reason: "end_turn" }, T0);
  assert.equal(t.lastAsstMsgAt, T0, "per-tab state, not per-app");
});

// --- hiding ---------------------------------------------------------------

test("a transient hide records the reason but leaves the turn armed", () => {
  const t = armedTab();
  S.applyHide(t, false, S.HIDE_REASONS.TAB_TEARDOWN);
  assert.equal(t.lastHideReason, "tab_teardown");
  assert.equal(t.waitingForAssistant, true);
  assert.equal(t.thinkingStartTs, T0);
});

test("hiding with no reason keeps the previous one", () => {
  const t = armedTab();
  S.applyHide(t, false, S.HIDE_REASONS.CLEAR);
  S.applyHide(t, true, "");
  assert.equal(t.lastHideReason, "clear", "an unattributed hide must not erase why");
});

test("noteBubbleShown marks the turn as having had a bubble", () => {
  const t = armedTab();
  S.noteBubbleShown(t);
  assert.equal(t.shownThisTurn, true);
});

// --- pty clock ------------------------------------------------------------

test("pty chunks record activity outside the post-message grace window", () => {
  const t = armedTab();
  assert.equal(S.notePtyChunk(t, T0 + 500), true);
  assert.equal(t.lastPtyChunkAt, T0 + 500);
});

test("pty chunks inside the grace window are ignored", () => {
  // claude repaints its own output right after a message lands; counting that
  // as activity would push the idle deadline out forever.
  const t = armedTab();
  S.noteAssistantRecord(t, {}, T0 + 1000);
  assert.equal(S.notePtyChunk(t, T0 + 1000 + S.PTY_POST_MSG_GRACE_MS - 1), false);
  assert.equal(t.lastPtyChunkAt, 0, "nothing recorded");
  assert.equal(S.notePtyChunk(t, T0 + 1000 + S.PTY_POST_MSG_GRACE_MS), true);
});

test("isPtyActive distinguishes a live tool call from a quiet wait", () => {
  const t = armedTab();
  assert.equal(S.isPtyActive(t, T0), false, "no chunk yet is not 'working'");
  S.notePtyChunk(t, T0 + 5000);
  assert.equal(S.isPtyActive(t, T0 + 5000 + S.PTY_ACTIVE_MS - 1), true);
  assert.equal(S.isPtyActive(t, T0 + 5000 + S.PTY_ACTIVE_MS), false);
});

test("switching back re-baselines the pty clock only for a live turn", () => {
  const live = armedTab();
  S.notePtyChunk(live, T0);
  assert.equal(S.rebaselinePtyClock(live, T0 + 60_000), true);
  assert.equal(live.lastPtyChunkAt, T0 + 60_000);
  assert.equal(
    S.shouldIdleTimeout(live, T0 + 60_000),
    false,
    "a live turn must survive being backgrounded for a minute"
  );

  const idle = S.newTabState();
  assert.equal(S.rebaselinePtyClock(idle, T0), false);
  assert.equal(idle.lastPtyChunkAt, 0);
});

// --- idle timeout + elapsed ----------------------------------------------

test("the idle timeout fires only after real pty silence", () => {
  const t = armedTab();
  S.notePtyChunk(t, T0);
  assert.equal(S.shouldIdleTimeout(t, T0 + S.THINKING_IDLE_MS), false);
  assert.equal(S.shouldIdleTimeout(t, T0 + S.THINKING_IDLE_MS + 1), true);
});

test("an unarmed tab never times out", () => {
  const t = S.newTabState();
  assert.equal(S.shouldIdleTimeout(t, T0 + 10 * S.THINKING_IDLE_MS), false);
});

test("elapsedSeconds counts from turn start and hides when idle", () => {
  const t = armedTab();
  assert.equal(S.elapsedSeconds(t, T0), 0);
  assert.equal(S.elapsedSeconds(t, T0 + 2999), 2, "floored, not rounded");
  S.applyHide(t, true, S.HIDE_REASONS.END_TURN);
  assert.equal(S.elapsedSeconds(t, T0 + 5000), null);
});

// --- full turn ------------------------------------------------------------

test("a whole healthy turn leaves no anomaly behind", () => {
  const t = S.newTabState();
  S.startTurn(t, T0);
  S.armTurn(t, T0);
  S.noteBubbleShown(t);
  S.notePtyChunk(t, T0 + 500);
  const chained = S.noteAssistantRecord(t, { stop_reason: null }, T0 + 1000);
  S.applyHide(t, chained.hide.endTurn, chained.hide.reason);
  S.noteBubbleShown(t);   // ensureThinkingBubble re-created it
  const done = S.noteAssistantRecord(t, { stop_reason: "end_turn" }, T0 + 4000);
  S.applyHide(t, done.hide.endTurn, done.hide.reason);
  assert.equal(t.waitingForAssistant, false);
  assert.equal(
    classifyBubbleAnomaly({
      role: "assistant",
      isReplay: false,
      isActive: true,
      bubbleConnected: true,
      shownThisTurn: t.shownThisTurn,
      lastHideReason: t.lastHideReason,
    }),
    null
  );
});

// --- contract with bubble_monitor.js -------------------------------------

test("every hide reason is one bubble_monitor.js can classify", () => {
  // A reason this module emits that the monitor does not know about would
  // silently degrade every anomaly for that cause to "unknown".
  const expected = {
    end_turn: "post_end_turn_continuation",
    transient: "transient_no_rearm",
    idle_timeout: "premature_idle_teardown",
    tab_teardown: "dom_teardown",
    clear: "dom_teardown",
  };
  for (const reason of Object.values(S.HIDE_REASONS)) {
    const anomaly = classifyBubbleAnomaly({
      role: "assistant",
      isReplay: false,
      isActive: true,
      bubbleConnected: false,
      shownThisTurn: true,
      lastHideReason: reason,
    });
    assert.ok(anomaly, `no classification for ${reason}`);
    assert.equal(anomaly.cause, expected[reason], `wrong cause for ${reason}`);
  }
});

test("a turn whose bubble never showed classifies as an arming gap", () => {
  const t = armedTab();
  const anomaly = classifyBubbleAnomaly({
    role: "assistant",
    isReplay: false,
    isActive: true,
    bubbleConnected: false,
    shownThisTurn: t.shownThisTurn,
    lastHideReason: t.lastHideReason,
  });
  assert.deepEqual(anomaly, { cause: "arming_gap" });
});
