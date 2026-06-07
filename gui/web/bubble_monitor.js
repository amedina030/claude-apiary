// bubble_monitor.js — pure anomaly classifier for the "thinking bubble".
//
// The bubble should be visible whenever claude is actively working — from the
// user's send until stop_reason=end_turn. It vanishes intermittently while
// claude is still producing messages, and the cause has been hard to pin down
// because it can't be reproduced on demand (see L-2026-133: this codebase has
// been burned by fixing GUI bugs against guessed fixtures). So instead of
// guessing, we MONITOR: at the moment a live assistant message lands we ask
// "was the bubble wrongly absent?" and, if so, classify WHY from how it last
// went down. A healthy turn keeps the bubble up until the message arrives, so
// it produces no anomaly — the log stays empty until the bug actually fires.
//
// Exposed in both environments:
//   - Browser: attaches to `window.apiaryBubbleMonitor`
//   - Node:    `module.exports = { classifyBubbleAnomaly }`

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.apiaryBubbleMonitor = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  // Snapshot taken the instant a transcript record arrives, BEFORE the bubble
  // state machine reacts to it:
  //   role            — arriving record role ("assistant" | "user" | ...)
  //   isReplay        — true for a transcript re-render (tab switch / reload);
  //                     replays are history, not live work → never an anomaly
  //   isActive        — record targets the active tab (the only tab that owns a
  //                     DOM bubble; background tabs intentionally have none)
  //   bubbleConnected — is the thinking bubble currently in the DOM
  //   shownThisTurn   — has the bubble been shown at any point this turn
  //   lastHideReason  — how the bubble last went down:
  //                     "end_turn" | "transient" | "idle_timeout" |
  //                     "tab_teardown" | "clear" | ""
  //
  // Returns null when there is no anomaly, else { cause }.
  function classifyBubbleAnomaly(s) {
    if (!s) return null;
    // Only a LIVE assistant message on the ACTIVE tab can evidence the bug:
    // claude emitted a message, so it was working, so the bubble should be up.
    if (s.isReplay) return null;
    if (s.role !== "assistant") return null;
    if (!s.isActive) return null;
    if (s.bubbleConnected) return null;   // healthy — bubble was up, no anomaly

    if (!s.shownThisTurn) {
      // The bubble never appeared for this turn at all — the turn was started
      // by something other than a plain-text composer send (slash command,
      // terminal-pane typing, a wakeup/continuation), so it was never armed.
      return { cause: "arming_gap" };
    }
    switch (s.lastHideReason) {
      case "idle_timeout":
        // The 15s pty-silence timeout tore the turn down during a quiet
        // stretch (silent tool call / long API-side think), then claude
        // resumed and nothing re-armed the bubble.
        return { cause: "premature_idle_teardown" };
      case "transient":
        // A chained non-end_turn message hid the bubble but the re-create
        // didn't take (e.g. flag cleared out from under it).
        return { cause: "transient_no_rearm" };
      case "end_turn":
        // claude continued producing after a message was marked end_turn.
        return { cause: "post_end_turn_continuation" };
      case "tab_teardown":
      case "clear":
        // A DOM teardown (tab switch / clear) removed the bubble and it was
        // not restored before the next message.
        return { cause: "dom_teardown" };
      default:
        return { cause: "unknown" };
    }
  }

  return { classifyBubbleAnomaly };
});
