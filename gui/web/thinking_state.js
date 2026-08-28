// thinking_state.js — the per-tab turn / thinking-bubble state machine.
//
// Extracted from app.js, where the same six fields were poked directly from
// nine places (appendMessage, the composer keydown, clearMessages,
// setActiveSession, appendPtyChunk, the 1s interval, startThinkingCounter,
// ensureThinkingBubble, hideThinkingBubbleFor). Every one of those was a
// chance to leave the turn armed with no bubble, or a bubble with no turn —
// the intermittent "thinking bubble vanished" bug bubble_monitor.js was built
// to observe. The transitions are pure functions over a plain TabState object
// so they can be Node-tested; app.js keeps the DOM (creating/removing the
// bubble element, the seconds badge) and calls in for every decision.
//
// A "turn" runs from the moment we learn claude is working (composer send,
// or a user/assistant record for turns that did not start in the composer)
// until stop_reason=end_turn or THINKING_IDLE_MS of pty silence.
//
// Exposed in both environments:
//   - Browser: attaches to `window.apiaryThinkingState`
//   - Node:    `module.exports = { newTabState, noteAssistantRecord, ... }`

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.apiaryThinkingState = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  // Turn ends after this much pty silence. Long enough to sit through an
  // API-side think or a silent tool call, short enough that a turn which
  // really ended without an end_turn record does not spin forever.
  const THINKING_IDLE_MS = 15000;
  // Pty chunks within this window mean "mid tool-call" — the dots switch from
  // pulsing in place to a wave cascade. Motion alone signals the state.
  const PTY_ACTIVE_MS = 2000;
  // After an assistant message lands, claude echoes its own output into the
  // pty. Those chunks are not evidence of new work, so they must not extend
  // the idle deadline — otherwise a finished turn never times out.
  const PTY_POST_MSG_GRACE_MS = 3000;

  // Why the bubble last went down. bubble_monitor.js classifies a missing
  // bubble from this value, so the two vocabularies must stay identical —
  // exported (and asserted in the tests) rather than re-typed as literals.
  const HIDE_REASONS = Object.freeze({
    END_TURN: "end_turn",
    TRANSIENT: "transient",
    IDLE_TIMEOUT: "idle_timeout",
    TAB_TEARDOWN: "tab_teardown",
    CLEAR: "clear",
  });

  // Per-tab, not per-app: a background tab's end_turn must update ITS turn,
  // not the visible one's. Lives until the session closes.
  function newTabState() {
    return {
      waitingForAssistant: false,
      thinkingStartTs: 0,
      lastPtyChunkAt: 0,
      lastAsstMsgAt: 0,
      // Monitor bookkeeping (see bubble_monitor.js): did the bubble appear at
      // all this turn, and how did it most recently go down. Both reset at
      // turn start so a missing-bubble event is attributed to the right turn.
      shownThisTurn: false,
      lastHideReason: "",
      // Unsent composer / quick-capture text. Not turn state, but per-tab for
      // the same reason: one shared <textarea> otherwise bleeds a half-typed
      // message into every other tab.
      draft: "",
      captureDraft: "",
    };
  }

  // Composer send of a fresh (non-queued) turn. Deliberately does NOT arm
  // waitingForAssistant — app.js flips that after the optimistic bubble is
  // rendered, so the render can tell "fresh" from "queued".
  function startTurn(t, now) {
    t.thinkingStartTs = now;
    t.shownThisTurn = false;
    t.lastHideReason = "";
  }

  // Mark the turn live: from here the bubble may spawn on pty chunks and the
  // idle timer runs. Separate from startTurn because the composer path arms
  // only after it has decided whether the message is fresh or queued.
  function armTurn(t, now) {
    if (t.waitingForAssistant) return false;
    t.waitingForAssistant = true;
    if (!t.thinkingStartTs) t.thinkingStartTs = now;
    return true;
  }

  // A live user record for a turn that did not start in the composer (slash
  // command, terminal-pane typing, a wakeup/continuation). Arms the turn at
  // its start so the bubble can show as soon as pty output flows — without
  // this the turn stayed unarmed until claude's first message, which is the
  // 'arming_gap' window the monitor flagged. A late-arriving user record for
  // a turn already in progress must not clobber it, hence the guard.
  function noteUserRecord(t, now) {
    if (t.waitingForAssistant) return false;
    t.shownThisTurn = false;
    t.lastHideReason = "";
    t.waitingForAssistant = true;
    t.thinkingStartTs = now;
    return true;
  }

  // A live assistant record. Mutates the timing fields and returns what the
  // caller must do to the DOM:
  //
  //   hide        {endTurn, reason} to pass straight to the hide path
  //   armed       true when this record started the turn for us
  //   ensureBubble the bubble should exist after this record (chained message
  //               mid-turn) — the caller only acts on it for the active tab
  //   flash       ping the taskbar (turn finished while backgrounded)
  //
  // `end_turn` ends the turn; anything else is a chained message WITHIN the
  // turn, and is definitive proof claude is still working — so it also arms a
  // turn nobody armed (see noteUserRecord).
  function noteAssistantRecord(t, msg, now) {
    t.lastAsstMsgAt = now;
    if (msg && msg.stop_reason === "end_turn") {
      return {
        hide: { endTurn: true, reason: HIDE_REASONS.END_TURN },
        armed: false,
        ensureBubble: false,
        flash: true,
      };
    }
    const armed = armTurn(t, now);
    return {
      hide: { endTurn: false, reason: HIDE_REASONS.TRANSIENT },
      armed,
      ensureBubble: true,
      flash: false,
    };
  }

  // The bookkeeping half of hiding the bubble. `endTurn=true` ends the turn
  // (real end_turn or the idle timeout); `false` is a transient hide between
  // chained messages. `reason` is remembered until the next turn starts, so a
  // later missing-bubble event can be attributed to this teardown.
  function applyHide(t, endTurn, reason) {
    if (reason) t.lastHideReason = reason;
    if (endTurn) {
      t.waitingForAssistant = false;
      t.thinkingStartTs = 0;
    }
  }

  function noteBubbleShown(t) {
    t.shownThisTurn = true;
  }

  // Claude echoes its own message into the pty right after it lands; those
  // chunks would otherwise keep resetting the idle deadline forever.
  function inPostMessageGrace(t, now) {
    return t.lastAsstMsgAt > 0 && now - t.lastAsstMsgAt < PTY_POST_MSG_GRACE_MS;
  }

  // Record pty activity. Returns false (and records nothing) inside the grace
  // window. Idempotent enough to call on every chunk.
  function notePtyChunk(t, now) {
    if (inPostMessageGrace(t, now)) return false;
    t.lastPtyChunkAt = now;
    return true;
  }

  // Switching back to a tab whose pty buffer was dropped while it was in the
  // background: without re-baselining, the idle timer measures 15s since
  // switch-OUT and kills a live turn the moment the user returns.
  function rebaselinePtyClock(t, now) {
    if (!t.waitingForAssistant) return false;
    t.lastPtyChunkAt = now;
    return true;
  }

  function isPtyActive(t, now) {
    return t.lastPtyChunkAt > 0 && now - t.lastPtyChunkAt < PTY_ACTIVE_MS;
  }

  // Seconds to show in the header badge, or null when the badge should hide.
  function elapsedSeconds(t, now) {
    if (!t.waitingForAssistant || t.thinkingStartTs <= 0) return null;
    return Math.floor((now - t.thinkingStartTs) / 1000);
  }

  // NOTE: an armed turn that has never seen a pty chunk (lastPtyChunkAt === 0)
  // times out on the first tick. That is intentional — it is the only signal
  // we have that a turn was armed for something that produces no pty output.
  function shouldIdleTimeout(t, now) {
    if (!t.waitingForAssistant) return false;
    return now - t.lastPtyChunkAt > THINKING_IDLE_MS;
  }

  return {
    THINKING_IDLE_MS,
    PTY_ACTIVE_MS,
    PTY_POST_MSG_GRACE_MS,
    HIDE_REASONS,
    newTabState,
    startTurn,
    armTurn,
    noteUserRecord,
    noteAssistantRecord,
    applyHide,
    noteBubbleShown,
    inPostMessageGrace,
    notePtyChunk,
    rebaselinePtyClock,
    isPtyActive,
    elapsedSeconds,
    shouldIdleTimeout,
  };
});
