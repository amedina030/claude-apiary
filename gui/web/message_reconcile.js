// message_reconcile.js — pure placement logic for the chat message list.
//
// Extracted from app.js's 179-line `appendMessage`, which mixed DOM building,
// per-tab turn state, the anomaly monitor and this decision in one function.
// What lives here is only the *decision*: given the incoming record and a
// plain-data view of the optimistic ("tentative") and queued user bubbles
// currently on screen, where does the new bubble go and what happens to the
// placeholders? app.js keeps the DOM: it reads the list into `view`, calls
// `planMessageInsert`, and applies the plan to real elements.
//
// Two problems it solves, both of which produced visibly wrong chronology:
//
//   1. Optimistic reconciliation. A composer send renders a tentative bubble
//      immediately; the JSONL-derived record arrives later. Replacing the
//      tentative IN PLACE (rather than removing it and appending at the end)
//      keeps the real user message above an assistant reply that landed in
//      between.
//   2. Queued ordering. Messages sent while claude was still working carry a
//      `queued` marker. The next assistant reply belongs to the PREVIOUS turn,
//      so it must be inserted before the first queued user message, and that
//      message then stops being queued — it is the current turn now.
//
// Exposed in both environments:
//   - Browser: attaches to `window.apiaryMessageReconcile`
//   - Node:    `module.exports = { planMessageInsert, stripFileManifest, ... }`

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.apiaryMessageReconcile = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  // The GUI appends a machine-readable file manifest to the text it sends, so
  // the JSONL record carries it but the optimistic render does not. Strip it
  // before display AND before match comparison, or every send with a staged
  // file would fall into the sweep path below. Mirrors the marker written by
  // file_refs.manifest_and_mark (Python side).
  const FILE_MANIFEST_MARKER = "[attached files — read these with the Read tool:]";

  function stripFileManifest(text) {
    if (!text) return text;
    const i = text.indexOf(FILE_MANIFEST_MARKER);
    if (i === -1) return text;
    return text.slice(0, i).replace(/\n+$/, "");
  }

  // A plan is always this shape, so callers never branch on "kind":
  //
  //   removeIndexes      indices into view.tentatives to delete from the DOM
  //   anchorIndex        insert the new bubble immediately AFTER this tentative
  //                      (-1 = no tentative anchor; caller falls back to
  //                      appending before the thinking bubble / at the end)
  //   inheritQueued      the new bubble takes over the `queued` marker of the
  //                      placeholder(s) it replaces
  //   unqueueFirstQueued insert before the first queued user bubble and clear
  //                      its marker (assistant path)
  //   warn               non-null when we could not match exactly — the caller
  //                      logs it so the next recurrence can be chased
  function emptyPlan() {
    return {
      removeIndexes: [],
      anchorIndex: -1,
      inheritQueued: false,
      unqueueFirstQueued: false,
      warn: null,
    };
  }

  // `msg`  — { role, text, matchText }. `text` is the record's raw text (its
  //          presence is what makes a user record reconcilable at all);
  //          `matchText` is the same text with the manifest stripped, which is
  //          what the optimistic bubble was rendered from and therefore the
  //          only fair thing to compare against. Defaults to `text`.
  // `view` — { tentatives: [{ text, queued }], hasQueuedUser: bool }
  function planMessageInsert(msg, view) {
    const plan = emptyPlan();
    const role = (msg && msg.role) || "";
    const tentatives = (view && view.tentatives) || [];

    if (role === "user" && msg.text) {
      const matchText = msg.matchText === undefined ? msg.text : msg.matchText;
      let matched = -1;
      for (let i = 0; i < tentatives.length; i++) {
        if (tentatives[i] && tentatives[i].text === matchText) {
          matched = i;
          break;
        }
      }
      if (matched !== -1) {
        plan.removeIndexes = [matched];
        plan.anchorIndex = matched;
        plan.inheritQueued = !!tentatives[matched].queued;
        return plan;
      }
      if (tentatives.length > 0) {
        // No exact text match but placeholders are outstanding. Something is
        // out of sync (content normalized between the optimistic render and
        // the JSONL write, or a stale tentative from a prior turn). Sweep them
        // all so we don't leave duplicates on screen, and hand the caller a
        // warning payload so the root cause is chaseable.
        plan.removeIndexes = tentatives.map((_, i) => i);
        plan.anchorIndex = tentatives.length - 1;
        plan.inheritQueued = tentatives.some((t) => t && t.queued);
        plan.warn = {
          tentativeTexts: tentatives.map((t) => (t ? t.text : undefined)),
          msgText: msg.text,
        };
        return plan;
      }
      return plan;
    }

    if (role === "assistant") {
      plan.unqueueFirstQueued = !!(view && view.hasQueuedUser);
      return plan;
    }

    return plan;
  }

  return {
    planMessageInsert,
    stripFileManifest,
    FILE_MANIFEST_MARKER,
  };
});
