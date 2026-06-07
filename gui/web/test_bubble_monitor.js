// Unit tests for bubble_monitor.js. Run with `node test_bubble_monitor.js`
// (no npm dependencies). The classifier is pure: given a snapshot taken when a
// transcript record arrives, it reports whether the thinking bubble was wrongly
// absent and why.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { classifyBubbleAnomaly } = require("./bubble_monitor");

// A healthy live assistant message: bubble was up when it landed.
const HEALTHY = {
  role: "assistant",
  isReplay: false,
  isActive: true,
  bubbleConnected: true,
  shownThisTurn: true,
  lastHideReason: "",
};

function withOverrides(o) {
  return Object.assign({}, HEALTHY, o);
}

// --- non-anomalies --------------------------------------------------------

test("healthy turn (bubble up when message lands) is not an anomaly", () => {
  assert.equal(classifyBubbleAnomaly(HEALTHY), null);
});

test("replay records are never anomalies", () => {
  assert.equal(classifyBubbleAnomaly(withOverrides({ isReplay: true, bubbleConnected: false })), null);
});

test("user messages are never anomalies", () => {
  assert.equal(classifyBubbleAnomaly(withOverrides({ role: "user", bubbleConnected: false })), null);
});

test("background-tab messages are never anomalies (no DOM bubble by design)", () => {
  assert.equal(classifyBubbleAnomaly(withOverrides({ isActive: false, bubbleConnected: false })), null);
});

test("null/undefined snapshot returns null", () => {
  assert.equal(classifyBubbleAnomaly(null), null);
  assert.equal(classifyBubbleAnomaly(undefined), null);
});

// --- anomalies, by cause --------------------------------------------------

test("arming gap: bubble never shown this turn", () => {
  const r = classifyBubbleAnomaly(withOverrides({ bubbleConnected: false, shownThisTurn: false }));
  assert.deepEqual(r, { cause: "arming_gap" });
});

test("arming gap takes priority over a stale hide reason", () => {
  // If the bubble was never shown this turn, the cause is the arming gap even
  // if lastHideReason carries a value from a prior turn.
  const r = classifyBubbleAnomaly(
    withOverrides({ bubbleConnected: false, shownThisTurn: false, lastHideReason: "end_turn" })
  );
  assert.deepEqual(r, { cause: "arming_gap" });
});

test("premature idle teardown: hidden by the 15s timeout, then a message", () => {
  const r = classifyBubbleAnomaly(
    withOverrides({ bubbleConnected: false, lastHideReason: "idle_timeout" })
  );
  assert.deepEqual(r, { cause: "premature_idle_teardown" });
});

test("transient hide that never re-armed", () => {
  const r = classifyBubbleAnomaly(
    withOverrides({ bubbleConnected: false, lastHideReason: "transient" })
  );
  assert.deepEqual(r, { cause: "transient_no_rearm" });
});

test("post-end_turn continuation", () => {
  const r = classifyBubbleAnomaly(
    withOverrides({ bubbleConnected: false, lastHideReason: "end_turn" })
  );
  assert.deepEqual(r, { cause: "post_end_turn_continuation" });
});

test("dom teardown (tab switch / clear)", () => {
  assert.deepEqual(
    classifyBubbleAnomaly(withOverrides({ bubbleConnected: false, lastHideReason: "tab_teardown" })),
    { cause: "dom_teardown" }
  );
  assert.deepEqual(
    classifyBubbleAnomaly(withOverrides({ bubbleConnected: false, lastHideReason: "clear" })),
    { cause: "dom_teardown" }
  );
});

test("unknown: shown then hidden with no recorded reason", () => {
  const r = classifyBubbleAnomaly(
    withOverrides({ bubbleConnected: false, lastHideReason: "" })
  );
  assert.deepEqual(r, { cause: "unknown" });
});
