// Unit tests for message_reconcile.js. Run with `node test_message_reconcile.js`
// (no npm dependencies), or via `pytest gui/test_js_suites.py`.
//
// The fixtures model what appendMessage sees: a list of optimistic ("tentative")
// user bubbles, some marked queued, and the record that just arrived.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  planMessageInsert,
  stripFileManifest,
  FILE_MANIFEST_MARKER,
} = require("./message_reconcile");

// --- helpers --------------------------------------------------------------

function view(tentatives, hasQueuedUser) {
  return {
    tentatives: tentatives.map((t) =>
      typeof t === "string" ? { text: t, queued: false } : t
    ),
    hasQueuedUser: !!hasQueuedUser,
  };
}

function user(text, matchText) {
  return matchText === undefined
    ? { role: "user", text }
    : { role: "user", text, matchText };
}

const ASSISTANT = { role: "assistant", text: "sure thing" };

// --- user records: optimistic reconciliation ------------------------------

test("an exact match replaces that tentative in place", () => {
  const plan = planMessageInsert(user("hello"), view(["hello"]));
  assert.deepEqual(plan.removeIndexes, [0]);
  assert.equal(plan.anchorIndex, 0, "the new bubble takes the tentative's slot");
  assert.equal(plan.inheritQueued, false);
  assert.equal(plan.warn, null);
});

test("the FIRST matching tentative wins when two have the same text", () => {
  // Sending the same text twice in a turn: the older placeholder is the one
  // this record belongs to, so the newer must survive for the next record.
  const plan = planMessageInsert(user("again"), view(["again", "again"]));
  assert.deepEqual(plan.removeIndexes, [0]);
  assert.equal(plan.anchorIndex, 0);
});

test("a matched tentative hands over its queued marker", () => {
  const plan = planMessageInsert(
    user("second"),
    view([{ text: "second", queued: true }])
  );
  assert.equal(plan.inheritQueued, true, "the real bubble stays queued");
});

test("a match ignores unrelated tentatives and leaves them alone", () => {
  const plan = planMessageInsert(user("b"), view(["a", "b", "c"]));
  assert.deepEqual(plan.removeIndexes, [1]);
  assert.equal(plan.anchorIndex, 1);
});

test("no tentatives at all is a plain append", () => {
  const plan = planMessageInsert(user("hello"), view([]));
  assert.deepEqual(plan.removeIndexes, []);
  assert.equal(plan.anchorIndex, -1);
  assert.equal(plan.unqueueFirstQueued, false);
  assert.equal(plan.warn, null);
});

test("a mismatch sweeps every tentative and warns", () => {
  // Whatever went out of sync, leaving placeholders on screen would duplicate
  // the message. Sweep, anchor at the last one's slot, and hand back a payload
  // to log so the root cause is chaseable next recurrence.
  const plan = planMessageInsert(user("normalized"), view(["typed", "other"]));
  assert.deepEqual(plan.removeIndexes, [0, 1]);
  assert.equal(plan.anchorIndex, 1);
  assert.ok(plan.warn, "a mismatch must be reported");
  assert.deepEqual(plan.warn.tentativeTexts, ["typed", "other"]);
  assert.equal(plan.warn.msgText, "normalized");
});

test("a sweep inherits queued if ANY swept tentative was queued", () => {
  const plan = planMessageInsert(
    user("x"),
    view([{ text: "a", queued: false }, { text: "b", queued: true }])
  );
  assert.equal(plan.inheritQueued, true);
});

test("a user record with no text never touches the tentatives", () => {
  for (const text of ["", undefined, null]) {
    const plan = planMessageInsert({ role: "user", text }, view(["pending"]));
    assert.deepEqual(plan.removeIndexes, [], `text=${JSON.stringify(text)}`);
    assert.equal(plan.warn, null);
  }
});

test("matching uses matchText, so a file manifest does not force a sweep", () => {
  // The record on disk carries the manifest; the optimistic bubble never did.
  const raw = "look at this\n\n" + FILE_MANIFEST_MARKER + "\nD:/x/y.png";
  const plan = planMessageInsert(
    user(raw, stripFileManifest(raw)),
    view(["look at this"])
  );
  assert.deepEqual(plan.removeIndexes, [0]);
  assert.equal(plan.warn, null, "a manifest is not a mismatch");
});

test("the warning reports the RAW text, not the stripped match key", () => {
  const raw = "typed\n\n" + FILE_MANIFEST_MARKER + "\nD:/x/y.png";
  const plan = planMessageInsert(user(raw, stripFileManifest(raw)), view(["other"]));
  assert.equal(plan.warn.msgText, raw);
});

// --- assistant records: queued ordering -----------------------------------

test("an assistant reply lands before the first queued user message", () => {
  // Chronology must read [user1][reply1][user2], not [user1][user2][reply1].
  const plan = planMessageInsert(ASSISTANT, view([], true));
  assert.equal(plan.unqueueFirstQueued, true);
  assert.deepEqual(plan.removeIndexes, [], "assistant records remove nothing");
});

test("an assistant reply with nothing queued just appends", () => {
  const plan = planMessageInsert(ASSISTANT, view([], false));
  assert.equal(plan.unqueueFirstQueued, false);
  assert.equal(plan.anchorIndex, -1);
});

test("an assistant record ignores outstanding tentatives", () => {
  const plan = planMessageInsert(ASSISTANT, view(["still pending"], false));
  assert.deepEqual(plan.removeIndexes, []);
  assert.equal(plan.anchorIndex, -1);
});

// --- other roles / defensive ---------------------------------------------

test("an unknown role is inert", () => {
  const plan = planMessageInsert({ role: "system", text: "x" }, view(["a"], true));
  assert.deepEqual(plan.removeIndexes, []);
  assert.equal(plan.unqueueFirstQueued, false);
});

test("a missing view is treated as an empty list", () => {
  const plan = planMessageInsert(user("hello"), undefined);
  assert.deepEqual(plan.removeIndexes, []);
  assert.equal(plan.anchorIndex, -1);
});

// --- stripFileManifest ----------------------------------------------------

test("stripFileManifest removes the manifest and its trailing blank lines", () => {
  const raw = "do the thing\n\n" + FILE_MANIFEST_MARKER + "\n- D:/a/b.png";
  assert.equal(stripFileManifest(raw), "do the thing");
});

test("stripFileManifest is a no-op without the marker", () => {
  assert.equal(stripFileManifest("plain text"), "plain text");
  assert.equal(stripFileManifest(""), "");
  assert.equal(stripFileManifest(undefined), undefined);
});

test("stripFileManifest on a manifest-only message yields empty text", () => {
  assert.equal(stripFileManifest(FILE_MANIFEST_MARKER + "\nD:/a.png"), "");
});
