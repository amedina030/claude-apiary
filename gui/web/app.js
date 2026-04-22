// apiary GUI frontend.

(() => {
  // --- DOM handles ----------------------------------------------------------
  const tabsEl = document.getElementById("tabs");
  const messagesEl = document.getElementById("messages");
  const emptyStateEl = document.getElementById("empty-state");
  const emptyPickBtn = document.getElementById("empty-pick");
  const statusEl = document.getElementById("status");
  const totIn = document.getElementById("tot-in");
  const totOut = document.getElementById("tot-out");
  const totCacheR = document.getElementById("tot-cache-r");
  const totCacheW = document.getElementById("tot-cache-w");
  const totRaw = document.getElementById("tot-raw");
  const totWeighted = document.getElementById("tot-weighted");
  const modelNameEl = document.getElementById("model-name");
  const toastsEl = document.getElementById("toasts");
  const ptyStripWrapEl = document.getElementById("pty-strip-wrap");
  const ptyTermEl = document.getElementById("pty-term");
  const ptyToggleEl = document.getElementById("pty-toggle");
  const ptyToggleLabelEl = document.getElementById("pty-toggle-label");
  const thinkingSecondsEl = document.getElementById("thinking-seconds");
  const promptBannerEl = document.getElementById("prompt-banner");
  const handoffBannerEl = document.getElementById("handoff-banner");
  const handoffBannerTextEl = document.getElementById("handoff-banner-text");
  const handoffBannerBtnEl = document.getElementById("handoff-banner-btn");
  const handoffBannerDismissEl = document.getElementById("handoff-banner-dismiss");
  const inputEl = document.getElementById("input");
  const INPUT_PLACEHOLDER_DEFAULT = inputEl.getAttribute("placeholder") || "";
  const sidebarSearchEl = document.getElementById("sidebar-search");
  const sidebarListEl = document.getElementById("sidebar-list");

  // --- tab bar --------------------------------------------------------------
  // Backend sends onSessions(list) whenever tabs open/close/switch. We render
  // a chip per session plus a "+" button that opens a folder picker.

  function renderTabs(sessions) {
    if (!Array.isArray(sessions)) return;
    tabsEl.innerHTML = "";
    // Empty-state toggle: when no tabs, hide messages list and show the
    // "pick a directory" CTA. When there are tabs, flip it back.
    if (sessions.length === 0) {
      emptyStateEl.classList.remove("hidden");
      messagesEl.style.display = "none";
    } else {
      emptyStateEl.classList.add("hidden");
      messagesEl.style.display = "";
    }
    for (const s of sessions) {
      const tab = document.createElement("div");
      tab.className = "tab" + (s.active ? " active" : "");
      tab.dataset.sid = s.session_id;
      // Surface active per-tab permissions in the tooltip + via data flags
      // so CSS can show a subtle dot/highlight when a tab is permissive.
      const flags = [];
      if (s.accept_edits) flags.push("auto-accept edits");
      if (s.pending_permission) flags.push("permission prompt waiting");
      tab.title = flags.length ? `${s.cwd}\n[${flags.join(", ")}]` : s.cwd;
      if (s.accept_edits) tab.dataset.acceptEdits = "1";
      if (s.pending_permission) tab.dataset.pendingPermission = "1";

      const label = document.createElement("span");
      label.className = "tab-label";
      label.textContent = s.label || s.cwd || "session";
      tab.appendChild(label);

      const close = document.createElement("button");
      close.className = "tab-close";
      close.type = "button";
      close.textContent = "×";
      close.title = "close tab";
      close.addEventListener("click", (e) => {
        e.stopPropagation();
        closeTab(s.session_id);
      });
      tab.appendChild(close);

      tab.addEventListener("click", () => {
        if (!s.active) switchTab(s.session_id);
      });
      tabsEl.appendChild(tab);
    }
    const addBtn = document.createElement("button");
    addBtn.className = "tab-new";
    addBtn.type = "button";
    addBtn.textContent = "+";
    addBtn.title = "open a new session in another directory";
    addBtn.addEventListener("click", openNewTab);
    tabsEl.appendChild(addBtn);
  }
  window.renderTabs = renderTabs;

  async function switchTab(sid) {
    if (!bridgeReady()) return;
    try { await window.pywebview.api.switch_session(sid); } catch (e) { console.error(e); }
  }

  async function closeTab(sid) {
    if (!bridgeReady()) return;
    try { await window.pywebview.api.close_session(sid); } catch (e) { console.error(e); }
  }

  async function openNewTab() {
    if (!bridgeReady()) return;
    try {
      const dir = await window.pywebview.api.pick_directory();
      if (!dir) return;
      await window.pywebview.api.open_session(dir);
    } catch (e) {
      console.error("open new tab failed", e);
    }
  }
  emptyPickBtn.addEventListener("click", openNewTab);

  // --- chat state -----------------------------------------------------------
  const totals = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
  const seen = new Set();
  // Sticky-bottom semantics: once the user scrolls up, we stop auto-scrolling
  // until they manually scroll back to bottom. This survives smooth-scroll
  // races and content-visibility layout timing better than re-checking on every
  // append.
  let sticky = true;
  let pendingScroll = false;
  let currentModel = "";

  // --- token weighting ------------------------------------------------------
  // Opus 4.x relative pricing (input-equivalent units):
  //   in:1 · out:5 · cache_r:0.1 · cache_w(5min):1.25
  function weightedTotal(t) {
    return t.input * 1 + t.output * 5 + t.cache_read * 0.1 + t.cache_write * 1.25;
  }
  function rawTotal(t) {
    return t.input + t.output + t.cache_read + t.cache_write;
  }
  function fmtNum(n) {
    return Math.round(n).toLocaleString();
  }

  function fmtTimestamp(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }

  function humanizeModel(id) {
    if (!id) return "—";
    // claude-opus-4-7 → opus 4.7 ; claude-sonnet-4-6-1m → sonnet 4.6 (1m)
    const m = id.match(/claude-([a-z]+)-(\d+)-(\d+)(?:\[(\w+)\])?/i);
    if (!m) return id;
    const variant = m[4] ? ` (${m[4]})` : "";
    return `${m[1]} ${m[2]}.${m[3]}${variant}`;
  }

  function setModel(model) {
    if (!model || model === currentModel) return;
    currentModel = model;
    modelNameEl.textContent = humanizeModel(model);
  }

  // --- markdown render (Phase 1 minimal — markdown-it lands later) ---------
  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function renderInline(text) {
    return escapeHtml(text).replace(/`([^`\n]+)`/g, "<code>$1</code>");
  }
  // GFM-ish table: leading + trailing pipe on every row (strict), so ambiguous
  // prose like "a | b" doesn't accidentally match. Separator row encodes per-
  // column alignment via :--- / ---: / :---:.
  const TABLE_RE = /(?:^|\n)(\|[^\n]+\|[ \t]*\n\|[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|[ \t]*\n(?:\|[^\n]+\|[ \t]*(?:\n|$))+)/g;
  function splitTableRow(row) {
    return row.replace(/^\s*\|/, "").replace(/\|[ \t]*$/, "").split("|").map((c) => c.trim());
  }
  function tableAlign(sep) {
    const s = sep.trim();
    const l = s.startsWith(":"), r = s.endsWith(":");
    if (l && r) return "center";
    if (r) return "right";
    if (l) return "left";
    return "";
  }
  function renderTable(block) {
    const lines = block.replace(/\n$/, "").split("\n");
    const header = splitTableRow(lines[0]);
    const aligns = splitTableRow(lines[1]).map(tableAlign);
    const body = lines.slice(2).map(splitTableRow);
    const cell = (text, align, tag) => {
      const style = align ? ` style="text-align:${align}"` : "";
      return `<${tag}${style}>${renderInline(text)}</${tag}>`;
    };
    let html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
    for (let i = 0; i < header.length; i++) html += cell(header[i] || "", aligns[i] || "", "th");
    html += "</tr></thead><tbody>";
    for (const row of body) {
      html += "<tr>";
      for (let i = 0; i < header.length; i++) html += cell(row[i] || "", aligns[i] || "", "td");
      html += "</tr>";
    }
    html += "</tbody></table></div>";
    return html;
  }
  function renderBlocks(text) {
    let html = "";
    let last = 0;
    let m;
    TABLE_RE.lastIndex = 0;
    while ((m = TABLE_RE.exec(text)) !== null) {
      // The regex's leading \n (if any) belongs to the pre-table segment.
      const tableStart = m.index + (text[m.index] === "\n" ? 1 : 0);
      html += renderInline(text.slice(last, tableStart));
      html += renderTable(m[1]);
      last = m.index + m[0].length;
    }
    html += renderInline(text.slice(last));
    return html;
  }
  function renderBody(text) {
    const fence = /```([\w-]*)\n([\s\S]*?)```/g;
    let html = "";
    let last = 0;
    let m;
    while ((m = fence.exec(text)) !== null) {
      html += renderBlocks(text.slice(last, m.index));
      html += `<pre><code class="lang-${escapeHtml(m[1] || "")}">${escapeHtml(m[2])}</code></pre>`;
      last = m.index + m[0].length;
    }
    html += renderBlocks(text.slice(last));
    return html;
  }

  // --- scroll handling ------------------------------------------------------
  function isAtBottom(tol = 32) {
    return messagesEl.scrollTop + messagesEl.clientHeight + tol >= messagesEl.scrollHeight;
  }
  // When we scroll programmatically the browser still fires a scroll event,
  // and the subsequent isAtBottom() reading can be stale (content-visibility
  // layout hasn't settled) — causing `sticky` to flip false and block the
  // next auto-scroll. Gate the sticky update to user-originated scrolls.
  let programmaticScroll = false;
  function scrollToBottom() {
    programmaticScroll = true;
    messagesEl.scrollTop = messagesEl.scrollHeight;
    // Clear on the microtask after the scroll event drains.
    setTimeout(() => { programmaticScroll = false; }, 0);
  }
  messagesEl.addEventListener("scroll", () => {
    if (programmaticScroll) return;
    sticky = isAtBottom();
  });
  function maybeScroll() {
    if (!sticky) return;
    if (pendingScroll) return;
    pendingScroll = true;
    // Double-rAF so layout (incl. content-visibility expansion) settles first.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        pendingScroll = false;
        scrollToBottom();
      });
    });
  }

  function setStatus(text) {
    if (!text) {
      statusEl.classList.add("hidden");
      statusEl.textContent = "";
    } else {
      statusEl.classList.remove("hidden");
      statusEl.textContent = text;
    }
  }

  // --- token totals ---------------------------------------------------------
  function refreshTotalsBadges() {
    totIn.textContent = totals.input.toLocaleString();
    totOut.textContent = totals.output.toLocaleString();
    totCacheR.textContent = totals.cache_read.toLocaleString();
    totCacheW.textContent = totals.cache_write.toLocaleString();
    totRaw.textContent = fmtNum(rawTotal(totals));
    totWeighted.textContent = fmtNum(weightedTotal(totals));
  }
  function addTokens(t) {
    if (!t) return;
    totals.input += t.input || 0;
    totals.output += t.output || 0;
    totals.cache_read += t.cache_read || 0;
    totals.cache_write += t.cache_write || 0;
    refreshTotalsBadges();
  }
  function resetTotals() {
    totals.input = totals.output = totals.cache_read = totals.cache_write = 0;
    refreshTotalsBadges();
  }
  function clearMessages() {
    messagesEl.innerHTML = "";
    seen.clear();
    resetTotals();
    sticky = true;
    currentModel = "";
    modelNameEl.textContent = "—";
  }

  // --- message render -------------------------------------------------------
  function appendMessage(msg) {
    if (msg.uuid && seen.has(msg.uuid)) return;
    if (msg.uuid) seen.add(msg.uuid);

    // Optimistic-render reconciliation: when a real user message arrives with
    // text that matches an outstanding tentative, replace the tentative in
    // place so the chronology holds. Previously we removed the tentative and
    // appended the real message at the end — but if an assistant message had
    // already landed between the tentative and the reconciliation, the real
    // user message would drop *below* the assistant reply.
    let insertAnchor = null;
    let inheritedQueued = false;
    if (msg.role === "user" && msg.text) {
      const tentatives = messagesEl.querySelectorAll("li.msg.user.tentative");
      let matched = null;
      for (const el of tentatives) {
        if (el.dataset.text === msg.text) {
          matched = el;
          break;
        }
      }
      if (matched) {
        insertAnchor = matched.nextSibling;
        inheritedQueued = matched.classList.contains("queued");
        matched.remove();
      } else if (tentatives.length > 0) {
        // No exact text match but outstanding tentatives exist. Something is
        // out of sync (content normalized between optimistic render and JSONL
        // write, or a stale tentative from a prior turn). Sweep them so we
        // don't leave duplicates on screen. Warn so the root cause can be
        // chased next recurrence.
        console.warn("[apiary] tentative reconcile fallback", {
          tentativeTexts: Array.from(tentatives, el => el.dataset.text),
          msgText: msg.text,
        });
        const last = tentatives[tentatives.length - 1];
        insertAnchor = last.nextSibling;
        inheritedQueued = Array.from(tentatives).some(el => el.classList.contains("queued"));
        tentatives.forEach(el => el.remove());
      }
    } else if (msg.role === "assistant") {
      // Queued-message ordering: if the user sent follow-up messages while
      // claude was still working on a prior turn, those messages are marked
      // .queued. Insert this reply BEFORE the first queued user message so
      // the chronology stays [user1][reply1][user2][reply2] instead of
      // [user1][user2][reply1][reply2]. Then un-mark that user message —
      // it's now the "current" turn, not a queued one, and the next
      // assistant reply should land after it (before the next queued msg).
      const firstQueued = messagesEl.querySelector("li.msg.user.queued");
      if (firstQueued) {
        insertAnchor = firstQueued;
        firstQueued.classList.remove("queued");
      }
    }

    const li = document.createElement("li");
    li.className = `msg ${msg.role}`;
    if (inheritedQueued) li.classList.add("queued");
    li.dataset.uuid = msg.uuid || "";

    const meta = document.createElement("div");
    meta.className = "msg-meta";

    const left = document.createElement("span");
    left.className = "msg-role";
    left.textContent = msg.role;
    meta.appendChild(left);

    const right = document.createElement("span");
    right.className = "msg-meta-right";
    const ts = document.createElement("span");
    ts.textContent = fmtTimestamp(msg.timestamp);
    right.appendChild(ts);

    if (msg.role === "assistant" && msg.tokens) {
      const toks = document.createElement("span");
      toks.className = "msg-tokens";
      toks.innerHTML =
        ` <span class="t">in ${msg.tokens.input}</span>` +
        ` <span class="t">out ${msg.tokens.output}</span>` +
        ` <span class="t">cache r ${msg.tokens.cache_read}</span>` +
        ` <span class="t">cache w ${msg.tokens.cache_write}</span>`;
      right.appendChild(toks);
    }
    meta.appendChild(right);
    li.appendChild(meta);

    // Click anywhere on meta row toggles per-message token visibility.
    meta.addEventListener("click", () => li.classList.toggle("expanded"));

    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = renderBody(msg.text || "");
    li.appendChild(body);

    if (insertAnchor && insertAnchor.parentNode === messagesEl) {
      messagesEl.insertBefore(li, insertAnchor);
    } else {
      messagesEl.appendChild(li);
    }

    if (msg.role === "assistant") {
      if (msg.tokens) addTokens(msg.tokens);
      if (msg.model) setModel(msg.model);
      lastAsstMsgAt = Date.now();
      // Route on stop_reason. "end_turn" = claude is done → kill the bubble
      // immediately. "tool_use" (or other mid-turn reasons) = more messages
      // still coming → hide the current DOM node but keep waitingForAssistant
      // true and re-spawn a fresh bubble right away so there's no visual gap
      // between chained assistant messages. The 15s idle timer is the final
      // safety net for sessions where stop_reason never resolves to end_turn.
      if (typeof hideThinkingBubble === "function") {
        if (msg.stop_reason === "end_turn") {
          hideThinkingBubble(true);
        } else {
          hideThinkingBubble(false);
          if (typeof ensureThinkingBubble === "function") ensureThinkingBubble();
        }
      }
      if (msg.stop_reason === "end_turn") {
        flashWindowIfBackground();
      }
    }

    maybeScroll();
  }

  // Optimistic render: synthesize a user message and append immediately on send.
  // Marked .tentative so the eventual JSONL-derived record replaces it cleanly.
  // `queued` marks messages the user sent while claude was still working on a
  // prior turn — the marker rides through reconciliation so assistant replies
  // land in the correct chronological position.
  function appendTentativeUserMessage(text, queued) {
    const li = document.createElement("li");
    li.className = "msg user tentative" + (queued ? " queued" : "");
    li.dataset.text = text;

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const left = document.createElement("span");
    left.className = "msg-role";
    left.textContent = "user";
    const right = document.createElement("span");
    right.className = "msg-meta-right";
    right.textContent = fmtTimestamp(new Date().toISOString());
    meta.appendChild(left);
    meta.appendChild(right);
    li.appendChild(meta);

    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = renderBody(text || "");
    li.appendChild(body);

    messagesEl.appendChild(li);
    maybeScroll();
  }

  function toast(text, kind) {
    const el = document.createElement("div");
    el.className = `toast ${kind || ""}`;
    el.textContent = text;
    toastsEl.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Insert text at the current caret position in the chat input and focus it.
  // Used by sidebar ID clicks so the user can compose messages that reference
  // notes ("see T-2026-153") without typing the ID by hand.
  function insertIntoInput(text) {
    if (!text) return;
    const start = inputEl.selectionStart ?? inputEl.value.length;
    const end = inputEl.selectionEnd ?? inputEl.value.length;
    const before = inputEl.value.slice(0, start);
    const after = inputEl.value.slice(end);
    // Pad with spaces so the ID doesn't glue to surrounding words.
    const needsLeadSpace = before.length > 0 && !/\s$/.test(before);
    const needsTrailSpace = after.length > 0 && !/^\s/.test(after);
    const insertText = (needsLeadSpace ? " " : "") + text + (needsTrailSpace ? " " : "");
    inputEl.value = before + insertText + after;
    const caret = start + insertText.length;
    inputEl.focus();
    inputEl.setSelectionRange(caret, caret);
  }

  // --- sidebar --------------------------------------------------------------
  let allNotes = [];
  let sidebarFilter = "";
  let sidebarSearchTimer = null;
  // Group key → collapsed boolean. Hydrated from backend on startup.
  let collapsed = new Set();
  // Notes the user has expanded inline. Survives sidebar re-renders (which
  // happen every 5s as the aggregator polls).
  const expandedNotes = new Set();
  // Cached body text keyed by note display_id, so re-renders don't re-fetch.
  const bodyCache = new Map();

  // Group definitions: each entry { key, label, match }. Order = render order.
  const GROUPS = [
    { key: "todo-ready", label: "todo (ready)", match: (n) => n.type === "todo" && (n.status === "active" || n.status === "open" || n.status === "") },
    { key: "todo-deferred", label: "todo (deferred)", match: (n) => n.type === "todo" && (n.status === "deferred" || n.status === "blocked") },
    { key: "wishlist", label: "wishlist", match: (n) => n.type === "wishlist" },
    { key: "blocker", label: "blocker", match: (n) => n.type === "blocker" },
    { key: "context", label: "context", match: (n) => n.type === "context" },
    { key: "decision", label: "decision", match: (n) => n.type === "decision" },
    { key: "reference", label: "reference", match: (n) => n.type === "reference" },
    { key: "general", label: "general", match: (n) => n.type === "general" },
    { key: "handoff", label: "handoff", match: (n) => n.type === "handoff" },
    { key: "learning", label: "learning", match: (n) => n.type === "learning" },
  ];

  function persistCollapsed() {
    if (!bridgeReady()) return;
    try {
      window.pywebview.api.save_sidebar_collapsed(Array.from(collapsed));
    } catch (e) {
      console.error("save_sidebar_collapsed failed", e);
    }
  }

  function renderSidebar() {
    const f = sidebarFilter.trim().toLowerCase();
    const filtered = f
      ? allNotes.filter(
          (n) =>
            (n.summary || "").toLowerCase().includes(f) ||
            (n.display_id || "").toLowerCase().includes(f) ||
            (n.repo_label || "").toLowerCase().includes(f)
        )
      : allNotes;

    const buckets = new Map();
    for (const g of GROUPS) buckets.set(g.key, []);
    for (const n of filtered) {
      for (const g of GROUPS) {
        if (g.match(n)) {
          buckets.get(g.key).push(n);
          break;
        }
      }
    }

    const frag = document.createDocumentFragment();
    for (const g of GROUPS) {
      const items = buckets.get(g.key);
      if (items.length === 0) continue;

      const group = document.createElement("div");
      group.className = "sidebar-group";
      if (collapsed.has(g.key)) group.classList.add("collapsed");

      const head = document.createElement("div");
      head.className = "sidebar-group-head";

      const left = document.createElement("span");
      const chev = document.createElement("span");
      chev.className = "chevron";
      chev.textContent = "▾";
      left.appendChild(chev);
      const label = document.createElement("span");
      label.textContent = g.label;
      left.appendChild(label);

      const right = document.createElement("span");
      right.textContent = String(items.length);

      head.appendChild(left);
      head.appendChild(right);

      head.addEventListener("click", () => {
        if (collapsed.has(g.key)) collapsed.delete(g.key);
        else collapsed.add(g.key);
        group.classList.toggle("collapsed");
        persistCollapsed();
      });

      group.appendChild(head);

      const body = document.createElement("div");
      body.className = "sidebar-group-body";
      for (const n of items) {
        const item = document.createElement("div");
        item.className = "sidebar-item";
        item.dataset.bodyPath = n.body_path || "";
        item.dataset.displayId = n.display_id;

        const idEl = document.createElement("div");
        idEl.className = "sidebar-item-id";
        idEl.textContent = n.display_id;
        idEl.title = "click to insert this ID into the chat input";
        idEl.addEventListener("click", (e) => {
          e.stopPropagation();
          insertIntoInput(n.display_id);
        });
        item.appendChild(idEl);

        const sumEl = document.createElement("div");
        sumEl.className = "sidebar-item-summary";
        // Prefer brief_summary — it's the one-sentence headline produced by
        // scribe's derivation, so mid-word cutoffs like "permissi" don't
        // leak into the sidebar. Fall back to the legacy `summary` for any
        // note that predates backfill (should be none after migration).
        sumEl.textContent = n.brief_summary || n.summary || "(no summary)";
        item.appendChild(sumEl);

        const repoEl = document.createElement("div");
        repoEl.className = "sidebar-item-repo";
        repoEl.textContent = n.repo_label;
        item.appendChild(repoEl);

        const bodyEl = document.createElement("pre");
        bodyEl.className = "sidebar-item-body";
        item.appendChild(bodyEl);

        // Restore expansion state across re-renders (aggregator polls every 5s).
        if (expandedNotes.has(n.display_id)) {
          item.classList.add("expanded");
          if (bodyCache.has(n.display_id)) {
            bodyEl.textContent = bodyCache.get(n.display_id);
            bodyEl.dataset.loaded = "1";
          } else if (n.has_body) {
            // Cache miss but the user wanted it expanded — kick off a load.
            loadNoteBody(n, bodyEl);
          } else {
            bodyEl.textContent = n.summary || "(no body)";
          }
        }

        item.addEventListener("click", (e) => {
          // Allow text selection inside an open body without collapsing it.
          if (item.classList.contains("expanded") && bodyEl.contains(e.target)) return;
          toggleNote(item, n, bodyEl);
        });
        body.appendChild(item);
      }
      group.appendChild(body);
      frag.appendChild(group);
    }

    sidebarListEl.innerHTML = "";
    sidebarListEl.appendChild(frag);
  }

  // --- usage meters (T-2026-25) --------------------------------------------
  // Backend (gui/usage_fetcher.UsagePoller) hits the undocumented
  // /api/oauth/usage endpoint every 60s and pushes the payload here via
  // window.apiary.onUsage. Payload shape:
  //   { five_hour: {utilization, resets_at},
  //     seven_day: {utilization, resets_at},
  //     extra_usage: {used_credits, monthly_limit, utilization, currency}, ... }
  // Null means the fetch failed (missing creds / 429 / timeout); we show
  // stale numbers with a "stale" tint rather than flashing empty.
  const usageMetersEl = document.getElementById("usage-meters");
  const usageBodyEl = document.getElementById("usage-body");
  const usageVariantToggleEl = document.getElementById("usage-variant-toggle");
  const usageCreditsToggleEl = document.getElementById("usage-credits-toggle");
  const usageRefreshBtnEl = document.getElementById("usage-refresh-btn");
  const usageTitleEl = document.getElementById("usage-title");
  let lastUsagePayload = null;
  let usageStale = false;
  // Credits visibility is purely a view preference — persist in localStorage
  // so it survives reload without needing a backend round-trip. Default OFF.
  let showCredits = (() => {
    try { return localStorage.getItem("apiary.usage.showCredits") === "1"; }
    catch (_) { return false; }
  })();
  let usageCollapsed = (() => {
    try { return localStorage.getItem("apiary.usage.collapsed") === "1"; }
    catch (_) { return false; }
  })();

  function reflectCreditsToggle() {
    if (!usageCreditsToggleEl) return;
    usageCreditsToggleEl.classList.toggle("active", showCredits);
    usageCreditsToggleEl.setAttribute(
      "aria-pressed", showCredits ? "true" : "false"
    );
  }
  reflectCreditsToggle();

  function reflectUsageCollapsed() {
    if (usageMetersEl) usageMetersEl.classList.toggle("collapsed", usageCollapsed);
    if (usageTitleEl) usageTitleEl.setAttribute("aria-expanded", usageCollapsed ? "false" : "true");
  }
  reflectUsageCollapsed();

  if (usageTitleEl) {
    const toggleCollapsed = () => {
      usageCollapsed = !usageCollapsed;
      try { localStorage.setItem("apiary.usage.collapsed", usageCollapsed ? "1" : "0"); }
      catch (_) {}
      reflectUsageCollapsed();
    };
    usageTitleEl.addEventListener("click", toggleCollapsed);
    usageTitleEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggleCollapsed();
      }
    });
  }

  // Taskbar flash (T-2026-182) — Claude awaits user → flash our button
  // until the user foregrounds the window. Backend skips the flash when the
  // window is already foreground, so this is safe to call unconditionally.
  const flashToggleEl = document.getElementById("flash-toggle");
  let flashEnabled = (() => {
    try { return localStorage.getItem("apiary.flash.enabled") !== "0"; }
    catch (_) { return true; }
  })();
  function reflectFlashToggle() {
    if (!flashToggleEl) return;
    flashToggleEl.setAttribute("aria-pressed", flashEnabled ? "true" : "false");
  }
  reflectFlashToggle();
  if (flashToggleEl) {
    flashToggleEl.addEventListener("click", () => {
      flashEnabled = !flashEnabled;
      try { localStorage.setItem("apiary.flash.enabled", flashEnabled ? "1" : "0"); }
      catch (_) {}
      reflectFlashToggle();
    });
  }
  function flashWindowIfBackground() {
    if (!flashEnabled) return;
    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.flash_window)) return;
    try { window.pywebview.api.flash_window(); } catch (_) {}
  }

  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const mainEl = document.getElementById("main");
  let sidebarHidden = (() => {
    try { return localStorage.getItem("apiary.sidebar.hidden") === "1"; }
    catch (_) { return false; }
  })();
  function reflectSidebarHidden() {
    if (mainEl) mainEl.classList.toggle("sidebar-hidden", sidebarHidden);
    document.body.classList.toggle("sidebar-hidden", sidebarHidden);
    if (sidebarToggleEl) {
      sidebarToggleEl.setAttribute("aria-pressed", sidebarHidden ? "true" : "false");
      sidebarToggleEl.textContent = sidebarHidden ? "‹" : "›";
    }
  }
  reflectSidebarHidden();
  if (sidebarToggleEl) {
    sidebarToggleEl.addEventListener("click", () => {
      sidebarHidden = !sidebarHidden;
      try { localStorage.setItem("apiary.sidebar.hidden", sidebarHidden ? "1" : "0"); }
      catch (_) {}
      reflectSidebarHidden();
    });
  }

  if (usageVariantToggleEl) {
    usageVariantToggleEl.addEventListener("click", () => {
      const cur = usageMetersEl.dataset.variant === "rings" ? "rings" : "bars";
      usageMetersEl.dataset.variant = cur === "bars" ? "rings" : "bars";
      renderUsage(lastUsagePayload);  // re-render with the same data
    });
  }
  if (usageCreditsToggleEl) {
    usageCreditsToggleEl.addEventListener("click", () => {
      showCredits = !showCredits;
      try { localStorage.setItem("apiary.usage.showCredits", showCredits ? "1" : "0"); }
      catch (_) {}
      reflectCreditsToggle();
      renderUsage(lastUsagePayload);
    });
  }
  if (usageRefreshBtnEl) {
    usageRefreshBtnEl.addEventListener("click", async () => {
      if (usageRefreshBtnEl.classList.contains("spinning")) return;
      usageRefreshBtnEl.classList.add("spinning");
      try {
        await window.pywebview.api.refresh_usage();
      } catch (e) {
        console.error("refresh_usage failed", e);
      } finally {
        usageRefreshBtnEl.classList.remove("spinning");
      }
    });
  }

  function renderUsage(payload) {
    if (payload !== null && payload !== undefined) {
      lastUsagePayload = payload;
      usageStale = false;
    } else {
      usageStale = true;
    }
    usageMetersEl.classList.toggle("stale", usageStale);
    if (lastUsagePayload === null) {
      usageBodyEl.textContent = "—";
      return;
    }
    const variant = usageMetersEl.dataset.variant === "rings" ? "rings" : "bars";
    const buckets = bucketsFromPayload(lastUsagePayload);
    usageBodyEl.innerHTML = "";
    if (variant === "rings") {
      usageBodyEl.appendChild(renderUsageRings(buckets));
    } else {
      usageBodyEl.appendChild(renderUsageBars(buckets));
    }
  }

  function bucketsFromPayload(p) {
    const rows = [
      { key: "5h", label: "5-hour", bucket: p.five_hour },
      { key: "7d", label: "7-day",  bucket: p.seven_day },
    ];
    const extra = p.extra_usage;
    if (extra && extra.is_enabled && showCredits) {
      const used = (extra.used_credits || 0) / 100;
      const limit = (extra.monthly_limit || 0) / 100;
      const cur = extra.currency || "USD";
      rows.push({
        key: "credits",
        label: "credits",
        bucket: {
          utilization: extra.utilization || 0,
          resets_at: null,
          sublabel: `${formatMoney(used, cur)} / ${formatMoney(limit, cur)}`,
        },
      });
    }
    return rows;
  }

  function formatMoney(amount, currency) {
    const sign = currency === "USD" ? "$" : "";
    return sign + amount.toFixed(2);
  }

  // "4h 52m", "2d 3h", "<1m", or "expired" for negative deltas.
  function formatResetIn(isoTs) {
    if (!isoTs) return "";
    const target = Date.parse(isoTs);
    if (!target) return "";
    const delta = target - Date.now();
    if (delta <= 0) return "reset due";
    const m = Math.floor(delta / 60000);
    if (m < 1) return "<1m";
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const remM = m % 60;
    if (h < 24) return remM ? `${h}h ${remM}m` : `${h}h`;
    const d = Math.floor(h / 24);
    const remH = h % 24;
    return remH ? `${d}d ${remH}h` : `${d}d`;
  }

  function thresholdClass(pct, timePct) {
    // When a time-elapsed reference is available, the main indicator is
    // binary: "hot" when consumption is outrunning the clock (pct greater
    // than the red bar's pct), "cool" otherwise. Ties resolve to cool —
    // being exactly on pace is not a warning state.
    //
    // Fallback (no time reference — e.g. the credits bucket) preserves the
    // original absolute thresholds: cool / warm / hot at 50% / 80%.
    if (typeof timePct === "number") {
      return pct > timePct ? "warm" : "cool";
    }
    if (pct >= 80) return "hot";
    if (pct >= 50) return "warm";
    return "cool";
  }

  // Nominal reset-window length in ms, keyed by bucket.key. Drives the thin
  // secondary indicator that tracks how far into the current window we are.
  // Credits bucket has no time-reset cadence and is intentionally omitted.
  const USAGE_WINDOW_MS = {
    "5h": 5 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
  };

  // Percent of the reset window that has elapsed. Returns a number in [0, 100]
  // or null when it cannot be computed (missing or unparseable timestamp,
  // unknown bucket key). Null triggers the caller to skip rendering the
  // secondary indicator rather than show a stale/guessed value.
  function elapsedWindowPct(isoTs, windowMs) {
    if (!isoTs || !windowMs) return null;
    const target = Date.parse(isoTs);
    if (!target) return null;
    const remaining = target - Date.now();
    const elapsed = windowMs - remaining;
    const pct = (elapsed / windowMs) * 100;
    return Math.max(0, Math.min(100, pct));
  }

  // Variant 1 — stacked horizontal bars.
  function renderUsageBars(rows) {
    const wrap = document.createElement("div");
    wrap.className = "usage-bars";
    for (const row of rows) {
      const pct = Math.max(0, Math.min(100, Number(row.bucket.utilization) || 0));
      const timePct = elapsedWindowPct(row.bucket.resets_at, USAGE_WINDOW_MS[row.key]);
      const item = document.createElement("div");
      item.className = "usage-bar-row";
      const title = document.createElement("div");
      title.className = "usage-bar-title";
      const left = document.createElement("span");
      left.textContent = row.label;
      const right = document.createElement("span");
      right.className = "usage-bar-pct";
      right.textContent = `${pct.toFixed(0)}%`;
      title.appendChild(left);
      title.appendChild(right);
      item.appendChild(title);
      const track = document.createElement("div");
      track.className = `usage-bar-track ${thresholdClass(pct, timePct)}`;
      const fill = document.createElement("div");
      fill.className = "usage-bar-fill";
      fill.style.width = `${pct}%`;
      track.appendChild(fill);
      item.appendChild(track);
      if (timePct !== null) {
        const timeTrack = document.createElement("div");
        timeTrack.className = "usage-bar-time";
        const timeFill = document.createElement("div");
        timeFill.className = "usage-bar-time-fill";
        timeFill.style.width = `${timePct}%`;
        timeTrack.appendChild(timeFill);
        item.appendChild(timeTrack);
      }
      const foot = document.createElement("div");
      foot.className = "usage-bar-foot";
      const resetsIn = formatResetIn(row.bucket.resets_at);
      foot.textContent = row.bucket.sublabel
        || (resetsIn ? `resets in ${resetsIn}` : "");
      item.appendChild(foot);
      wrap.appendChild(item);
    }
    return wrap;
  }

  // Variant 2 — side-by-side SVG donut rings.
  const RING_R = 18;
  const RING_CIRC = 2 * Math.PI * RING_R;
  function renderUsageRings(rows) {
    const wrap = document.createElement("div");
    wrap.className = "usage-rings";
    for (const row of rows) {
      const pct = Math.max(0, Math.min(100, Number(row.bucket.utilization) || 0));
      const timePct = elapsedWindowPct(row.bucket.resets_at, USAGE_WINDOW_MS[row.key]);
      const cell = document.createElement("div");
      cell.className = `usage-ring-cell ${thresholdClass(pct, timePct)}`;
      const resetsIn = formatResetIn(row.bucket.resets_at);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 44 44");
      svg.setAttribute("class", "usage-ring-svg");
      const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      track.setAttribute("class", "usage-ring-track");
      track.setAttribute("cx", "22"); track.setAttribute("cy", "22"); track.setAttribute("r", String(RING_R));
      svg.appendChild(track);
      const fill = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      fill.setAttribute("class", "usage-ring-fill");
      fill.setAttribute("cx", "22"); fill.setAttribute("cy", "22"); fill.setAttribute("r", String(RING_R));
      fill.setAttribute("stroke-dasharray", String(RING_CIRC));
      fill.setAttribute("stroke-dashoffset", String(RING_CIRC * (1 - pct / 100)));
      fill.setAttribute("transform", "rotate(-90 22 22)");
      svg.appendChild(fill);
      if (timePct !== null) {
        const TIME_R = 21;
        const TIME_CIRC = 2 * Math.PI * TIME_R;
        const timeTrack = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        timeTrack.setAttribute("class", "usage-ring-time-track");
        timeTrack.setAttribute("cx", "22"); timeTrack.setAttribute("cy", "22"); timeTrack.setAttribute("r", String(TIME_R));
        svg.appendChild(timeTrack);
        const timeFill = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        timeFill.setAttribute("class", "usage-ring-time-fill");
        timeFill.setAttribute("cx", "22"); timeFill.setAttribute("cy", "22"); timeFill.setAttribute("r", String(TIME_R));
        timeFill.setAttribute("stroke-dasharray", String(TIME_CIRC));
        timeFill.setAttribute("stroke-dashoffset", String(TIME_CIRC * (1 - timePct / 100)));
        timeFill.setAttribute("transform", "rotate(-90 22 22)");
        svg.appendChild(timeFill);
      }
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("class", "usage-ring-text");
      text.setAttribute("x", "22"); text.setAttribute("y", "22");
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("dominant-baseline", "central");
      text.textContent = `${pct.toFixed(0)}%`;
      svg.appendChild(text);
      cell.appendChild(svg);
      const label = document.createElement("div");
      label.className = "usage-ring-label";
      label.textContent = row.label;
      cell.appendChild(label);
      const sub = document.createElement("div");
      sub.className = "usage-ring-sub";
      sub.textContent = row.bucket.sublabel || (resetsIn ? `in ${resetsIn}` : "");
      cell.appendChild(sub);
      wrap.appendChild(cell);
    }
    return wrap;
  }

  // Re-tick every 30s so the "resets in Nh Mm" countdown stays current even
  // between backend pushes.
  setInterval(() => { if (lastUsagePayload) renderUsage(lastUsagePayload); }, 30000);

  async function loadNoteBody(note, bodyEl) {
    if (!note.body_path || !bridgeReady()) {
      bodyEl.textContent = note.summary || "(no body)";
      return;
    }
    bodyEl.textContent = "loading…";
    try {
      const body = await window.pywebview.api.get_note_body(note.body_path);
      const text = body || "(empty)";
      bodyEl.textContent = text;
      bodyEl.dataset.loaded = "1";
      bodyCache.set(note.display_id, text);
    } catch (e) {
      bodyEl.textContent = `(failed to read body: ${e})`;
    }
  }

  function toggleNote(item, note, bodyEl) {
    if (item.classList.contains("expanded")) {
      item.classList.remove("expanded");
      expandedNotes.delete(note.display_id);
      return;
    }
    item.classList.add("expanded");
    expandedNotes.add(note.display_id);
    if (!note.has_body) {
      const text = note.summary || "(no body)";
      bodyEl.textContent = text;
      bodyCache.set(note.display_id, text);
      return;
    }
    if (bodyEl.dataset.loaded === "1") return;
    if (bodyCache.has(note.display_id)) {
      bodyEl.textContent = bodyCache.get(note.display_id);
      bodyEl.dataset.loaded = "1";
      return;
    }
    loadNoteBody(note, bodyEl);
  }
  sidebarSearchEl.addEventListener("input", (e) => {
    sidebarFilter = e.target.value;
    if (sidebarSearchTimer) clearTimeout(sidebarSearchTimer);
    sidebarSearchTimer = setTimeout(renderSidebar, 200);
  });

  // --- pty terminal (xterm.js) ---------------------------------------------
  // xterm.js renders the pty stream as a real terminal, so ANSI escapes, cursor
  // repaints, and spinner redraws work correctly instead of leaving visible
  // garbage like the previous "stripAnsi-then-show-last-N-lines" approach.
  //
  // After an assistant message lands, claude repaints its status line, spinner
  // cleanup, tool result boxes, footer hints, etc. Don't treat those chunks as
  // real pty activity for purposes of the 15s thinking-idle timeout — otherwise
  // turn-end detection gets extended by trailing repaints.
  const PTY_POST_MSG_GRACE_MS = 3000;
  let lastPtyChunkAt = 0;
  let lastAsstMsgAt = 0;

  // Read back CSS vars for xterm theme so a theme.json change propagates.
  function themeVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  if (typeof window.Terminal !== "function") {
    console.error("[apiary-gui] xterm.js failed to load (window.Terminal undefined).");
  }
  if (typeof window.FitAddon !== "object" || !window.FitAddon.FitAddon) {
    console.error("[apiary-gui] xterm FitAddon failed to load (window.FitAddon.FitAddon missing).");
  }

  const term = new window.Terminal({
    fontFamily: themeVar("--font-family", "Consolas, monospace"),
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    convertEol: false,
    cols: 120,
    rows: 30,
    theme: {
      background: themeVar("--pty-bg", "#010409"),
      foreground: themeVar("--pty-fg", "#c9d1d9"),
    },
  });
  const fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  // Defer term.open() until the terminal first has real size — opening on a
  // zero-height container leaves xterm's renderer un-initialized. Writes to
  // term BEFORE open still update the internal buffer (xterm parses them),
  // so the prompt detector can read the buffer the whole time even while the
  // terminal is visually collapsed.
  let termOpened = false;

  // ConPTY bracketed-paste: pywinpty wraps multi-byte writes in paste markers,
  // inside which "\r" is a literal newline rather than a submit. Route Enter
  // (CR) and Esc through the dedicated control paths to avoid that wrapping;
  // everything else (printable keys, arrows, Tab, Ctrl+*) goes raw via send_text.
  term.onData((data) => {
    if (!bridgeReady()) return;
    if (data === "\r") {
      window.pywebview.api.send_control("m");
      return;
    }
    if (data === "\x1b") {
      window.pywebview.api.send_escape();
      return;
    }
    window.pywebview.api.send_text(data);
  });

  // Refit and notify the backend of new rows/cols. Only meaningful while the
  // terminal is visible — a collapsed (height:0) container yields bogus sizes.
  function refitTerminal() {
    if (ptyStripWrapEl.classList.contains("collapsed")) return;
    try {
      fitAddon.fit();
    } catch (_) {
      return;
    }
    if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
      window.pywebview.api.pty_resize(term.rows, term.cols);
    }
  }
  window.addEventListener("resize", refitTerminal);

  function ensureTermOpen() {
    if (termOpened) return;
    // Open immediately, even into a 0-height collapsed container. The renderer
    // can't lay out yet, but xterm only updates buffer.active reliably AFTER
    // open() — empirically, write()-before-open leaves buffer.active stale
    // (despite xterm docs implying otherwise), which made the prompt detector
    // miss prompts that fired while the pty pane was collapsed. fit() runs
    // later via ResizeObserver once the container actually has size.
    term.open(ptyTermEl);
    termOpened = true;
    try { fitAddon.fit(); } catch (_) {}
    if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
      window.pywebview.api.pty_resize(term.rows, term.cols);
    }
  }
  // Fallback trigger: once the terminal's container actually has size, open.
  // This is the robust path — it doesn't care whether the size came from a
  // manual toggle, auto-expand, or the user resizing the window. Also keeps
  // calling fit() after open so that the CSS height transition (0 → 280px)
  // doesn't leave xterm stuck at the mid-transition row count.
  try {
    const ro = new ResizeObserver(() => {
      const h = ptyTermEl.clientHeight;
      if (!termOpened && h >= 10) {
        ensureTermOpen();
      } else if (termOpened && h >= 10) {
        try { fitAddon.fit(); } catch (_) {}
        if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
          window.pywebview.api.pty_resize(term.rows, term.cols);
        }
      }
    });
    ro.observe(ptyTermEl);
  } catch (_) { /* ResizeObserver unavailable — toggle path still works */ }

  // Open immediately so the buffer starts populating from the first pty byte,
  // even if the user never expands the terminal pane.
  ensureTermOpen();

  function ptyExpand() {
    ptyStripWrapEl.classList.remove("collapsed");
    // ResizeObserver will catch the height change and call ensureTermOpen. As
    // a safety net, retry a few times across the CSS transition window too.
    const retryUntil = Date.now() + 400;
    function tryOpen() {
      ensureTermOpen();
      if (!termOpened && Date.now() < retryUntil) requestAnimationFrame(tryOpen);
      else refitTerminal();
    }
    requestAnimationFrame(tryOpen);
  }
  function ptyCollapse() {
    ptyStripWrapEl.classList.add("collapsed");
  }

  function inPostMessageGrace() {
    return lastAsstMsgAt > 0 && Date.now() - lastAsstMsgAt < PTY_POST_MSG_GRACE_MS;
  }

  // --- thinking bubble ------------------------------------------------------
  // Three animated dots at the bottom of the chat while claude is working.
  // The turn stays "active" (waitingForAssistant=true) from the user-sent
  // Enter until either 15s of pty silence or a tab switch. Individual
  // assistant messages inside that window only remove the DOM bubble —
  // subsequent pty chunks in the same turn re-spawn it so long tool chains
  // don't look idle.
  const THINKING_IDLE_MS = 15000;
  let thinkingEl = null;
  let waitingForAssistant = false;
  // Wall-clock ms when the current turn's Enter landed; 0 while idle. Replaces
  // the old pty-unread chunk counter in the same DOM slot (T-2026-198).
  let thinkingStartTs = 0;

  function startThinkingCounter() {
    thinkingStartTs = Date.now();
    thinkingSecondsEl.textContent = "0s";
    thinkingSecondsEl.classList.remove("hidden");
  }

  function stopThinkingCounter() {
    thinkingStartTs = 0;
    thinkingSecondsEl.classList.add("hidden");
    thinkingSecondsEl.textContent = "";
  }

  function ensureThinkingBubble() {
    if (!waitingForAssistant) return;
    if (thinkingEl && thinkingEl.isConnected) return;
    thinkingEl = document.createElement("li");
    thinkingEl.className = "msg thinking";
    thinkingEl.innerHTML =
      '<div class="thinking-body">' +
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span>' +
      '</div>';
    messagesEl.appendChild(thinkingEl);
    maybeScroll();
  }

  function hideThinkingBubble(endTurn) {
    if (endTurn) {
      waitingForAssistant = false;
      stopThinkingCounter();
    }
    if (thinkingEl) {
      thinkingEl.remove();
      thinkingEl = null;
    }
  }

  setInterval(() => {
    if (!waitingForAssistant) return;
    if (thinkingStartTs > 0) {
      const secs = Math.floor((Date.now() - thinkingStartTs) / 1000);
      thinkingSecondsEl.textContent = secs + "s";
    }
    if (Date.now() - lastPtyChunkAt > THINKING_IDLE_MS) {
      hideThinkingBubble(true);
    }
  }, 1000);

  function appendPtyChunk(chunk) {
    // Always write — the buffer is valid pre-open, so the prompt detector can
    // read it even when the terminal is visually collapsed.
    term.write(chunk);
    scheduleDetect();
    ensureThinkingBubble();
    // Buffer is moving again → next time it goes stationary with an unparsed
    // prompt, the fallback should fire anew (not dedup against the stale
    // signature from the last stationary state).
    unknownPromptNotifiedSig = null;
    if (inPostMessageGrace()) return;
    lastPtyChunkAt = Date.now();
  }

  // Fallback auto-expand: if the pty has been stationary for a while AND the
  // buffer shows a numbered-option prompt that the detector *couldn't* parse
  // into a banner (unknown prompt type, new claude version, MCP OAuth, etc.),
  // reveal the terminal so the user isn't stuck. Active/streaming claude
  // doesn't trigger this — only "waiting" states do.
  const UNKNOWN_PROMPT_QUIET_MS = 2500;
  const UNKNOWN_PROMPT_CHECK_MS = 500;
  let unknownPromptNotifiedSig = null;
  setInterval(() => {
    if (!ptyStripWrapEl.classList.contains("collapsed")) return;
    if (activePrompt) return;               // banner is handling it
    if (lastPtyChunkAt === 0) return;        // nothing has happened yet
    if (Date.now() - lastPtyChunkAt < UNKNOWN_PROMPT_QUIET_MS) return; // still busy
    const lines = readScreenLines();
    // If the structured detector would *accept* this buffer, the detector
    // (with its dismiss-cooldown) is in charge — not the fallback. This
    // covers the common "user just answered, prompt text is still in the
    // buffer but we're in cooldown" case: previously the fallback fired even
    // though there was nothing the user needed to see.
    if (detectPrompt(lines)) return;
    // Now the only remaining case: numbered options are visible but the
    // structured parser rejected the shape. That's a genuinely unknown prompt
    // type → reveal the terminal.
    let hasNumbered = false;
    for (let i = lines.length - 1; i >= 0 && i >= lines.length - 20; i--) {
      if (/^\s*[❯>]\s*\d+\.\s+/.test(lines[i])) { hasNumbered = true; break; }
    }
    if (!hasNumbered) return;
    // Dedupe — fire exactly once per distinct stationary-with-prompt state.
    // Reset happens when appendPtyChunk runs (claude is doing things again).
    const sig = lines.slice(-10).join("\n");
    if (sig === unknownPromptNotifiedSig) return;
    unknownPromptNotifiedSig = sig;
    ptyExpand();
    toast("Claude is waiting on something the GUI couldn't parse — check the terminal.", "error");
  }, UNKNOWN_PROMPT_CHECK_MS);

  // Click on the bordered "terminal" label toggles the terminal panel.
  // The surrounding bar is reserved for drag-resize of the chat input
  // (see the gutter drag handler below) -- stop propagation so a click
  // on the label doesn't also start a drag on the bar.
  ptyToggleLabelEl.addEventListener("mousedown", (e) => e.stopPropagation());
  ptyToggleLabelEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (ptyStripWrapEl.classList.contains("collapsed")) ptyExpand();
    else ptyCollapse();
  });

  // --- prompt detector (reads xterm's screen buffer) -----------------------
  // Claude Code's interactive prompts (plan-mode approval, trust folder,
  // tool permission, etc.) share one visual shape:
  //
  //     <question text>
  //
  //     ❯ 1. <option text>
  //       2. <option text>
  //       3. <option text>
  //
  //     <optional footer with keyboard hints>
  //
  // We read xterm's post-render buffer (cursor movements already applied, so
  // no spinner garbage) and extract that structure. Match anywhere in the
  // buffer — prompts often live a few rows above the status line.
  // After the user answers or dismisses a prompt, claude takes ~300-800ms to
  // repaint the buffer (spinner frames, transition to feedback mode, etc.).
  // Suppress re-showing the same prompt during this window so the banner
  // doesn't immediately pop back for a prompt the user already dealt with.
  const DISMISS_COOLDOWN_MS = 4000;
  // Signatures the user has explicitly answered via a button click. Persists
  // for the session — once answered, the same prompt should never re-banner
  // even if claude leaves the rendered prompt in the xterm scrollback for
  // longer than DISMISS_COOLDOWN_MS. (Cancel-via-× still uses the cooldown,
  // since the user may genuinely want the prompt back.)
  const answeredSigs = new Set();
  let activePrompt = null;   // { question, options, signature }
  let awaitingFeedback = false;  // true while claude is in option-4 feedback mode
  let lastDismissedSig = null;
  let lastDismissedAt = 0;
  // MCP permission-prompt path (see scribe C-2026-36). When claude is
  // spawned with --permission-prompt-tool, prompts arrive here as
  // structured { pending_id, tool_name, input, tool_use_id } instead of
  // being scraped from the xterm buffer. Kept in its own state slot so
  // the two paths can't fight over promptBannerEl.
  let mcpPrompt = null;   // { pendingId, payload }
  // Parser lives in prompt_detector.js so it can be Node-tested. If the bundle
  // failed to load, fall back to a no-op stub so the rest of the UI still works.
  const detectPrompt = (window.apiaryPromptDetector && window.apiaryPromptDetector.detectPrompt) || (() => null);

  const SCREEN_SCAN_ROWS = 200;
  function readScreenLines() {
    const buf = term.buffer.active;
    if (!buf) return [];
    const total = buf.length;
    const start = Math.max(0, total - SCREEN_SCAN_ROWS);
    const lines = [];
    for (let y = start; y < total; y++) {
      const line = buf.getLine(y);
      if (!line) continue;
      lines.push(line.translateToString(true).replace(/\s+$/, ""));
    }
    return lines;
  }

  function renderPromptBanner(prompt) {
    promptBannerEl.innerHTML = "";
    // Block chat input while a prompt is pending — otherwise the user's text
    // lands in whatever input field the prompt is still showing (e.g. plan
    // mode's "tell claude what to change" feedback box).
    inputEl.classList.add("input-blocked");
    inputEl.placeholder = "Claude is waiting on a response — pick an option, or click ✕ to cancel";
    const close = document.createElement("button");
    close.className = "prompt-dismiss";
    close.type = "button";
    close.textContent = "✕";
    close.title = "cancel the prompt (sends Esc to claude) and hide this banner";
    close.addEventListener("click", () => hidePromptBanner({ cancel: true }));
    promptBannerEl.appendChild(close);

    // Question only in the banner — the plan body / context lives in the
    // chat (recorded the moment the prompt was detected), so repeating it
    // here would be visual noise right above the input.
    if (prompt.question) {
      const q = document.createElement("div");
      q.className = "prompt-question";
      q.textContent = prompt.question;
      promptBannerEl.appendChild(q);
    }
    const row = document.createElement("div");
    row.className = "prompt-options";
    for (const opt of prompt.options) {
      const btn = document.createElement("button");
      btn.className = "prompt-option" + (opt.selected ? " selected" : "");
      btn.type = "button";
      btn.textContent = `${opt.number}. ${opt.text}`;
      btn.addEventListener("click", () => answerPrompt(opt.number, opt.text));
      row.appendChild(btn);
    }
    promptBannerEl.appendChild(row);
    promptBannerEl.classList.remove("hidden");
    flashWindowIfBackground();
  }

  // Per-tool banner body renderers. Each takes the tool's `input` object
  // and an `edited` object to mutate, and returns a DocumentFragment. The
  // user can tweak the editable fields before clicking Allow; on allow,
  // `edited` is sent as the MCP `updatedInput`. A tool with no bespoke
  // renderer falls through to renderGenericInput (read-only JSON).
  const WRITE_CONTENT_PREVIEW_LINES = 20;

  function makeFilepathInput(edited, initial) {
    const el = document.createElement("input");
    el.type = "text";
    el.className = "prompt-filepath-input";
    el.value = initial || "";
    el.spellcheck = false;
    el.addEventListener("input", () => { edited.file_path = el.value; });
    return el;
  }

  function makeEditableTextarea(edited, key, initial, extraClass = "") {
    const ta = document.createElement("textarea");
    ta.className = "prompt-code prompt-editable" + (extraClass ? " " + extraClass : "");
    ta.value = initial || "";
    ta.spellcheck = false;
    ta.rows = Math.min(20, Math.max(1, (initial || "").split("\n").length));
    ta.addEventListener("input", () => { edited[key] = ta.value; });
    return ta;
  }

  function renderBashInput(input, edited) {
    const frag = document.createDocumentFragment();
    if (input.description) {
      const d = document.createElement("div");
      d.className = "prompt-subhead";
      d.textContent = input.description;
      frag.appendChild(d);
    }
    frag.appendChild(makeEditableTextarea(
      edited, "command",
      typeof input.command === "string" ? input.command : "",
      "prompt-bash-cmd",
    ));
    if (input.timeout) {
      const t = document.createElement("div");
      t.className = "prompt-meta";
      t.textContent = `timeout: ${input.timeout}ms`;
      frag.appendChild(t);
    }
    return frag;
  }

  function renderEditInput(input, edited) {
    const frag = document.createDocumentFragment();
    frag.appendChild(makeFilepathInput(edited, input.file_path));
    if (input.replace_all) {
      const badge = document.createElement("span");
      badge.className = "prompt-badge";
      badge.textContent = "replace_all";
      frag.appendChild(badge);
    }
    const grid = document.createElement("div");
    grid.className = "prompt-diff-grid";
    const oldCol = document.createElement("div");
    oldCol.className = "prompt-diff-col prompt-diff-old";
    const oldLbl = document.createElement("div");
    oldLbl.className = "prompt-diff-label";
    oldLbl.textContent = "− old";
    oldCol.appendChild(oldLbl);
    oldCol.appendChild(makeEditableTextarea(edited, "old_string", input.old_string || ""));
    const newCol = document.createElement("div");
    newCol.className = "prompt-diff-col prompt-diff-new";
    const newLbl = document.createElement("div");
    newLbl.className = "prompt-diff-label";
    newLbl.textContent = "+ new";
    newCol.appendChild(newLbl);
    newCol.appendChild(makeEditableTextarea(edited, "new_string", input.new_string || ""));
    grid.appendChild(oldCol);
    grid.appendChild(newCol);
    frag.appendChild(grid);
    return frag;
  }

  function renderWriteInput(input, edited) {
    const frag = document.createDocumentFragment();
    frag.appendChild(makeFilepathInput(edited, input.file_path));
    const content = typeof input.content === "string" ? input.content : "";
    const lines = content.split("\n");
    const truncated = lines.length > WRITE_CONTENT_PREVIEW_LINES;
    const ta = makeEditableTextarea(
      edited, "content",
      truncated ? lines.slice(0, WRITE_CONTENT_PREVIEW_LINES).join("\n") : content,
    );
    frag.appendChild(ta);
    if (truncated) {
      const hidden = lines.length - WRITE_CONTENT_PREVIEW_LINES;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "prompt-showmore";
      btn.textContent = `show ${hidden} more line${hidden === 1 ? "" : "s"}`;
      btn.addEventListener("click", () => {
        ta.value = content;
        edited.content = content;
        ta.rows = Math.min(30, lines.length);
        btn.remove();
      });
      frag.appendChild(btn);
    }
    return frag;
  }

  function renderGenericInput(input) {
    // Unknown tool shape — render read-only to avoid shipping malformed
    // JSON back as updatedInput. Known-shape tools above are editable.
    const frag = document.createDocumentFragment();
    const pre = document.createElement("pre");
    pre.className = "prompt-code";
    try { pre.textContent = JSON.stringify(input, null, 2); } catch (_) { pre.textContent = ""; }
    frag.appendChild(pre);
    return frag;
  }

  function renderToolInput(toolName, input, edited) {
    switch (toolName) {
      case "Bash": return renderBashInput(input, edited);
      case "Edit": return renderEditInput(input, edited);
      case "Write": return renderWriteInput(input, edited);
      default: return renderGenericInput(input);
    }
  }

  const TOOL_EDITABLE = new Set(["Bash", "Edit", "Write"]);

  function renderMcpPromptBanner(pendingId, payload) {
    // Structured permission request from the MCP bridge. Does NOT go
    // through prompt_detector — no TUI scraping involved.
    promptBannerEl.innerHTML = "";
    inputEl.classList.add("input-blocked");
    inputEl.placeholder = "Claude is waiting on a permission decision — Allow or Deny";

    const close = document.createElement("button");
    close.className = "prompt-dismiss";
    close.type = "button";
    close.textContent = "✕";
    close.title = "deny and hide this banner";
    close.addEventListener("click", () => resolveMcpPrompt("deny", "user dismissed"));
    promptBannerEl.appendChild(close);

    const q = document.createElement("div");
    q.className = "prompt-question";
    const toolName = (payload && payload.tool_name) || "unknown";
    q.textContent = `Claude wants to use ${toolName}`;
    promptBannerEl.appendChild(q);

    const input = (payload && payload.input) || {};
    // `edited` mirrors `input`. Per-field renderers mutate it as the user
    // types; on allow, it becomes the MCP `updatedInput` (so claude sees
    // the tweaked command / file path / etc. instead of the original).
    mcpPrompt.edited = { ...input };
    const body = document.createElement("div");
    body.className = "prompt-toolbody";
    body.appendChild(renderToolInput(toolName, input, mcpPrompt.edited));
    promptBannerEl.appendChild(body);

    const row = document.createElement("div");
    row.className = "prompt-options";
    const allow = document.createElement("button");
    allow.className = "prompt-option selected";
    allow.type = "button";
    allow.textContent = "Allow";
    allow.addEventListener("click", () => resolveMcpPrompt("allow"));
    row.appendChild(allow);
    const deny = document.createElement("button");
    deny.className = "prompt-option";
    deny.type = "button";
    deny.textContent = "Deny";
    deny.addEventListener("click", () => resolveMcpPrompt("deny"));
    row.appendChild(deny);
    promptBannerEl.appendChild(row);

    promptBannerEl.classList.remove("hidden");
    flashWindowIfBackground();
  }

  function resolveMcpPrompt(behavior, message = "") {
    const current = mcpPrompt;
    if (!current) return;
    mcpPrompt = null;
    promptBannerEl.classList.add("hidden");
    promptBannerEl.innerHTML = "";
    inputEl.classList.remove("input-blocked");
    inputEl.placeholder = INPUT_PLACEHOLDER_DEFAULT;
    if (!bridgeReady()) return;
    // On allow with an editable tool, forward the user's edits as
    // updatedInput so claude sees the tweaked args. Deny ignores edits.
    let updatedInput = null;
    if (behavior === "allow") {
      const toolName = (current.payload && current.payload.tool_name) || "";
      if (TOOL_EDITABLE.has(toolName) && current.edited) {
        updatedInput = current.edited;
      }
    }
    try {
      window.pywebview.api.resolve_permission(
        current.pendingId, behavior, message, updatedInput,
      );
    } catch (e) {
      console.error("resolve_permission failed", e);
    }
  }

  function hidePromptBanner({ cancel = false } = {}) {
    // When the user explicitly dismisses the banner, also send Esc to the pty
    // so the underlying prompt is actually cancelled — otherwise the user
    // starts typing in the chat and their text lands in whatever input field
    // the prompt is still showing.
    if (cancel && bridgeReady()) {
      try { window.pywebview.api.send_escape(); } catch (_) {}
    }
    if (activePrompt) {
      lastDismissedSig = activePrompt.signature;
      lastDismissedAt = Date.now();
    }
    promptBannerEl.classList.add("hidden");
    promptBannerEl.innerHTML = "";
    activePrompt = null;
    awaitingFeedback = false;
    inputEl.classList.remove("input-blocked");
    inputEl.placeholder = INPUT_PLACEHOLDER_DEFAULT;
  }

  function promptUuid(sig) {
    // Deterministic id per prompt signature so the "appeared" record and the
    // later resolution update target the same DOM element.
    return `prompt-${btoa(unescape(encodeURIComponent(sig))).replace(/[^A-Za-z0-9]/g, "").slice(0, 32)}`;
  }

  function recordPromptAppeared(prompt) {
    const uuid = promptUuid(prompt.signature);
    if (messagesEl.querySelector(`li[data-uuid="${uuid}"]`)) return;
    const parts = [];
    if (prompt.question) parts.push(`**${prompt.question}**`);
    if (prompt.context) parts.push("", prompt.context);
    appendMessage({
      uuid,
      role: "prompt",
      text: parts.join("\n"),
      timestamp: new Date().toISOString(),
    });
  }

  function recordPromptResolution(prompt, chosenOpt) {
    const uuid = promptUuid(prompt.signature);
    const existing = messagesEl.querySelector(`li[data-uuid="${uuid}"]`);
    if (existing) {
      // Append the choice to the existing record instead of creating a second
      // entry — keeps history as one block per prompt interaction.
      const body = existing.querySelector(".msg-body");
      if (body && !existing.dataset.resolved) {
        const choice = document.createElement("div");
        choice.className = "prompt-choice";
        choice.innerHTML = "→ chose: <strong></strong>";
        choice.querySelector("strong").textContent =
          `${chosenOpt.number}. ${chosenOpt.text}`;
        body.appendChild(choice);
        existing.dataset.resolved = "1";
      }
      return;
    }
    // Fallback — shouldn't normally fire, but if the "appeared" record never
    // went out for some reason, at least record the resolution.
    appendMessage({
      uuid: `${uuid}-resolved`,
      role: "prompt",
      text: `→ chose: **${chosenOpt.number}. ${chosenOpt.text}**`,
      timestamp: new Date().toISOString(),
    });
  }

  function answerPrompt(digit, optText = "") {
    if (!bridgeReady() || !activePrompt) return;
    const chosenOpt = activePrompt.options.find(o => o.number === digit)
                   || { number: digit, text: optText };
    recordPromptResolution(activePrompt, chosenOpt);
    // Feedback-entry options (plan mode's "4. Tell Claude what to change") put
    // claude into a text-input sub-mode. We send the digit to enter that
    // mode, skip the Enter, hide the banner, and unblock the chat input so
    // the user can type their feedback and submit it with Enter as normal.
    const isFeedback = /^\s*tell\b|what to change|with this feedback/i.test(optText);
    // Permanently mute this signature for the session — claude often leaves
    // the answered prompt rendered in the buffer past DISMISS_COOLDOWN_MS,
    // and we don't want the banner to re-appear after the user has already
    // committed to a choice.
    answeredSigs.add(activePrompt.signature);
    window.pywebview.api.send_text(String(digit));
    if (isFeedback) {
      awaitingFeedback = true;
      hidePromptBanner();
      inputEl.placeholder = "Type your feedback for claude — Enter to submit, Esc to cancel";
      inputEl.focus();
    } else {
      setTimeout(() => window.pywebview.api.send_control("m"), 30);
      hidePromptBanner();
    }
  }

  function runDetect() {
    const found = detectPrompt(readScreenLines());
    if (!found) {
      if (activePrompt) hidePromptBanner();
      // Also clear the dismiss cooldown — if the prompt is truly gone, a
      // later recurrence is a NEW prompt and deserves a banner.
      if (lastDismissedSig && (!activePrompt)) {
        lastDismissedSig = null;
        lastDismissedAt = 0;
      }
      return;
    }
    if (activePrompt && activePrompt.signature === found.signature) return;
    // Already-answered signatures stay muted for the rest of the session,
    // independent of the dismiss-cooldown window.
    if (answeredSigs.has(found.signature)) return;
    // Suppress re-show if the user just dismissed this exact prompt — claude
    // takes a moment to repaint, and the stale buffer would fire the banner
    // right back in the user's face.
    if (found.signature === lastDismissedSig &&
        Date.now() - lastDismissedAt < DISMISS_COOLDOWN_MS) {
      return;
    }
    activePrompt = found;
    recordPromptAppeared(found);
    renderPromptBanner(found);
  }

  // Steady-state poll instead of chunk-triggered debounce. Claude Code's
  // spinner repaints continuously while a prompt is waiting for input, so a
  // debounce keyed on appendPtyChunk never quiesces and the detector never
  // fires (T-2026-171, T-2026-172). A 500ms poll runs independently of chunk
  // churn; detectPrompt is cheap (regex over ~200 buffer rows).
  const PROMPT_DETECT_INTERVAL_MS = 500;
  setInterval(runDetect, PROMPT_DETECT_INTERVAL_MS);
  function scheduleDetect() { /* retained as hook point; poll now drives detection */ }

  // --- handoff banner (T-2026-164) ------------------------------------------
  // Shown on GUI start when core/startup.py reports unfilled handoffs. Button
  // types `/backfill-handoffs` into the composer (user presses Enter to submit).
  // Manual dismiss only — auto-hide was aggressive if the user alt-tabbed during
  // launch, and the × button is always available.
  function hideHandoffBanner() {
    handoffBannerEl.classList.add("hidden");
  }
  function showHandoffBanner(count) {
    const n = Number(count) || 0;
    if (n <= 0) { hideHandoffBanner(); return; }
    handoffBannerTextEl.textContent =
      `${n} previous session${n === 1 ? "" : "s"} not yet summarized.`;
    handoffBannerEl.classList.remove("hidden");
  }
  handoffBannerBtnEl.addEventListener("click", () => {
    inputEl.value = "/backfill-handoffs";
    inputEl.focus();
    hideHandoffBanner();
  });
  handoffBannerDismissEl.addEventListener("click", hideHandoffBanner);

  // --- agents strip (ephemeral) --------------------------------------------
  // Thin row between tabs and header. Only renders when there's ≥1 running
  // or ≥1 recently-finished agent; stays gone otherwise. Data shape matches
  // SubagentTracker's AgentState.to_dict() (see gui/subagent_tracker.py).
  const agentsStripEl = document.getElementById("agents-strip");
  const agentsChipsEl = document.getElementById("agents-chips");
  const agentsClearBtnEl = document.getElementById("agents-clear-btn");
  const agentsCancelBtnEl = document.getElementById("agents-cancel-btn");
  const agentsDrawerEl = document.getElementById("agents-drawer");
  let lastAgentsPayload = [];
  let selectedAgentId = "";
  const agentsDismissed = (() => {
    try {
      const raw = localStorage.getItem("apiary.agents.dismissed");
      const arr = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(arr) ? arr : []);
    } catch (_) { return new Set(); }
  })();
  function persistAgentsDismissed() {
    try { localStorage.setItem("apiary.agents.dismissed", JSON.stringify(Array.from(agentsDismissed))); }
    catch (_) {}
  }

  function fmtAgentTokens(n) {
    if (!Number.isFinite(n) || n <= 0) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
  }
  function fmtAgentElapsed(startedAtEpoch, nowMs) {
    const secs = Math.max(0, Math.floor((nowMs - startedAtEpoch * 1000) / 1000));
    if (secs < 60) return secs + "s";
    if (secs < 3600) return Math.floor(secs / 60) + "m" + (secs % 60) + "s";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return h + "h" + m + "m";
  }
  function fmtAgentDuration(startedAt, lastActivity) {
    const secs = Math.max(0, Math.floor(lastActivity - startedAt));
    if (secs < 60) return secs + "s";
    if (secs < 3600) return Math.floor(secs / 60) + "m" + (secs % 60) + "s";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return h + "h" + m + "m";
  }
  function shortModel(model) {
    const m = /claude-(opus|sonnet|haiku)/i.exec(model || "");
    return m ? m[1].toLowerCase() : (model || "").slice(0, 8);
  }
  function agentTokenTotal(a) {
    const t = a.tokens || {};
    return (t.input | 0) + (t.output | 0) + (t.cache_read | 0) + (t.cache_write | 0);
  }
  function buildAgentChip(agent) {
    const chip = document.createElement("span");
    chip.className = "chip" + (agent.status === "running" ? "" : " done");
    if (agent.agent_id === selectedAgentId) chip.classList.add("selected");
    chip.dataset.agentId = agent.agent_id;

    const dot = document.createElement("span");
    dot.className = "dot " + (agent.status || "running");
    chip.appendChild(dot);

    const type = document.createElement("span");
    type.className = "type";
    type.textContent = agent.subagent_type || "(agent)";
    chip.appendChild(type);

    const meta = document.createElement("span");
    meta.className = "meta";
    const tot = agentTokenTotal(agent);
    meta.textContent = (agent.status === "running"
      ? fmtAgentElapsed(agent.started_at, Date.now())
      : fmtAgentDuration(agent.started_at, agent.last_activity_at)
    ) + " · " + fmtAgentTokens(tot);
    chip.appendChild(meta);

    if (agent.status === "running" && agent.current_tool) {
      const cur = document.createElement("span");
      cur.className = "cur";
      cur.textContent = "· " + agent.current_tool;
      chip.appendChild(cur);
    }
    if (agent.status !== "running") {
      const x = document.createElement("button");
      x.type = "button";
      x.className = "chip-dismiss";
      x.setAttribute("aria-label", "dismiss agent");
      x.textContent = "×";
      chip.appendChild(x);
    }
    return chip;
  }
  function renderAgentDrawer() {
    if (!agentsDrawerEl) return;
    if (!selectedAgentId) {
      agentsDrawerEl.classList.add("hidden");
      agentsDrawerEl.innerHTML = "";
      agentsDrawerEl.dataset.agentId = "";
      return;
    }
    const agent = lastAgentsPayload.find((a) => a && a.agent_id === selectedAgentId);
    if (!agent) {
      agentsDrawerEl.classList.add("hidden");
      agentsDrawerEl.innerHTML = "";
      agentsDrawerEl.dataset.agentId = "";
      return;
    }
    agentsDrawerEl.classList.remove("hidden");

    const hist = agent.tool_histogram || {};
    const histEntries = Object.entries(hist).sort((a, b) => b[1] - a[1]);
    const toolsStr = histEntries.map(([n, c]) => c + " " + n).join(" · ");
    const fields = [
      ["desc",     agent.description ? '"' + agent.description + '"' : ""],
      ["model",    shortModel(agent.model)],
      ["tools",    toolsStr],
      ["in",       fmtAgentTokens(agent.tokens?.input)],
      ["out",      fmtAgentTokens(agent.tokens?.output)],
      ["cache r",  fmtAgentTokens(agent.tokens?.cache_read)],
      ["cache w",  fmtAgentTokens(agent.tokens?.cache_write)],
      ["prompt",   agent.prompt_preview],
    ];

    // In-place update: if the drawer already renders this agent, only patch
    // the value spans whose text has changed. Full rebuild would kill any
    // in-progress text selection when backend pushes arrive (every ~2s while
    // an agent is burning tokens).
    const sameAgent = agentsDrawerEl.dataset.agentId === selectedAgentId;
    const existingRows = sameAgent ? agentsDrawerEl.querySelectorAll(".kv") : null;

    if (sameAgent && existingRows && existingRows.length === fields.filter(([, v]) => v !== "" && v !== null && v !== undefined).length) {
      // Row count matches — patch in place.
      let i = 0;
      for (const [, val] of fields) {
        if (val === "" || val === null || val === undefined) continue;
        const row = existingRows[i++];
        if (!row) break;
        const v = row.querySelector(".v");
        if (v && v.textContent !== String(val)) v.textContent = String(val);
      }
      const finEl = agentsDrawerEl.querySelector(".final");
      if (agent.final_text && agent.status !== "running") {
        if (finEl) {
          if (finEl.textContent !== agent.final_text) finEl.textContent = agent.final_text;
        } else {
          const fin = document.createElement("div");
          fin.className = "final";
          fin.textContent = agent.final_text;
          agentsDrawerEl.appendChild(fin);
        }
      } else if (finEl) {
        finEl.remove();
      }
      return;
    }

    // Different agent, or row count changed — full rebuild.
    agentsDrawerEl.innerHTML = "";
    agentsDrawerEl.dataset.agentId = selectedAgentId;
    for (const [label, val] of fields) {
      if (val === "" || val === null || val === undefined) continue;
      const r = document.createElement("div");
      r.className = "kv";
      const k = document.createElement("span"); k.className = "k"; k.textContent = label;
      const v = document.createElement("span"); v.className = "v"; v.textContent = String(val);
      r.appendChild(k); r.appendChild(v);
      agentsDrawerEl.appendChild(r);
    }
    if (agent.final_text && agent.status !== "running") {
      const fin = document.createElement("div");
      fin.className = "final";
      fin.textContent = agent.final_text;
      agentsDrawerEl.appendChild(fin);
    }
  }
  function renderAgentsStrip(payload) {
    lastAgentsPayload = Array.isArray(payload) ? payload : [];
    const running = [];
    const recent = [];
    for (const a of lastAgentsPayload) {
      if (!a || typeof a !== "object") continue;
      if (a.status === "running") running.push(a);
      else if (!agentsDismissed.has(a.agent_id)) recent.push(a);
    }
    recent.sort((a, b) => (b.last_activity_at || 0) - (a.last_activity_at || 0));

    if (running.length === 0 && recent.length === 0) {
      if (agentsStripEl) agentsStripEl.classList.add("hidden");
      if (agentsClearBtnEl) agentsClearBtnEl.classList.add("hidden");
      if (agentsCancelBtnEl) agentsCancelBtnEl.classList.add("hidden");
      if (selectedAgentId) { selectedAgentId = ""; renderAgentDrawer(); }
      return;
    }
    if (agentsStripEl) agentsStripEl.classList.remove("hidden");
    if (agentsClearBtnEl) agentsClearBtnEl.classList.toggle("hidden", recent.length === 0);
    if (agentsCancelBtnEl) agentsCancelBtnEl.classList.toggle("hidden", running.length === 0);
    if (agentsChipsEl) {
      agentsChipsEl.innerHTML = "";
      for (const a of running) agentsChipsEl.appendChild(buildAgentChip(a));
      for (const a of recent)  agentsChipsEl.appendChild(buildAgentChip(a));
    }
    // If the selected chip vanished (e.g. dismissed), drop the drawer.
    if (selectedAgentId && !lastAgentsPayload.some((a) => a && a.agent_id === selectedAgentId)) {
      selectedAgentId = "";
    }
    renderAgentDrawer();
  }
  function resetAgentsStripState() {
    lastAgentsPayload = [];
    selectedAgentId = "";
    renderAgentsStrip([]);
  }
  if (agentsChipsEl) {
    agentsChipsEl.addEventListener("click", (ev) => {
      const chip = ev.target.closest(".chip");
      if (!chip) return;
      const id = chip.dataset.agentId;
      if (!id) return;
      if (ev.target.closest(".chip-dismiss")) {
        ev.stopPropagation();
        agentsDismissed.add(id);
        persistAgentsDismissed();
        if (selectedAgentId === id) selectedAgentId = "";
        renderAgentsStrip(lastAgentsPayload);
        return;
      }
      selectedAgentId = (selectedAgentId === id) ? "" : id;
      renderAgentsStrip(lastAgentsPayload);
    });
  }
  if (agentsClearBtnEl) {
    agentsClearBtnEl.addEventListener("click", (ev) => {
      ev.stopPropagation();
      for (const a of lastAgentsPayload) {
        if (a && a.status !== "running") agentsDismissed.add(a.agent_id);
      }
      persistAgentsDismissed();
      renderAgentsStrip(lastAgentsPayload);
    });
  }
  if (agentsCancelBtnEl) {
    // Claude Code runs subagents in-process, so there's no per-agent kill.
    // Route through interruptClaudeSession() — a multi-ESC burst that
    // mirrors manual spam-clicking (single ESC lands unreliably when
    // tool calls are in flight; see claude-code issues #3455/#17466).
    agentsCancelBtnEl.addEventListener("click", (ev) => {
      ev.stopPropagation();
      interruptClaudeSession(agentsCancelBtnEl);
    });
  }
  // Live clock: update elapsed text in-place on running chips every second.
  // Must NOT rebuild DOM — that would nuke any in-progress text selection in
  // the expanded drawer (kept breaking copy-paste of final_text).
  setInterval(() => {
    if (!agentsChipsEl) return;
    if (!lastAgentsPayload.some((a) => a && a.status === "running")) return;
    const byId = new Map();
    for (const a of lastAgentsPayload) {
      if (a && a.agent_id) byId.set(a.agent_id, a);
    }
    const now = Date.now();
    for (const chip of agentsChipsEl.querySelectorAll(".chip")) {
      const a = byId.get(chip.dataset.agentId);
      if (!a || a.status !== "running") continue;
      const meta = chip.querySelector(".meta");
      if (!meta) continue;
      meta.textContent = fmtAgentElapsed(a.started_at, now) + " · " + fmtAgentTokens(agentTokenTotal(a));
    }
  }, 1000);

  // --- bridge surface (Python → JS) ----------------------------------------
  function bridgeReady() {
    return typeof window.pywebview !== "undefined" && window.pywebview.api;
  }

  // Interrupt helper — the ONLY way GUI features may cancel claude-code.
  //
  // SAFETY INVARIANT: this helper must never call send_control("c"). Ctrl+C
  // pressed twice quits claude-code and kills the whole pty session. Only
  // ESC (0x1b, "cancel current turn") and Ctrl+U (0x15, readline kill-line)
  // are used. If you add a new cancel surface, route it through here.
  //
  // Why a burst: single ESC lands unreliably when tool calls are in flight
  // (claude-code issues #3455, #17466, #21895). Empirically, spam-clicking
  // 3–4 times is what actually interrupts — this mirrors that cadence.
  let _interruptInFlight = false;
  function interruptClaudeSession(button) {
    if (_interruptInFlight) return;
    _interruptInFlight = true;
    if (button) button.classList.add("cancelling");
    const ESC_COUNT = 4;
    const ESC_INTERVAL_MS = 120;
    for (let i = 0; i < ESC_COUNT; i++) {
      setTimeout(() => {
        try { window.pywebview.api.send_escape(); } catch (_) {}
      }, i * ESC_INTERVAL_MS);
    }
    // Flush any lingering prompt text so the next input isn't concatenated.
    setTimeout(() => {
      try { window.pywebview.api.send_control("u"); } catch (_) {}
    }, ESC_COUNT * ESC_INTERVAL_MS + 50);
    // Release the debounce + visual state after the burst completes.
    setTimeout(() => {
      _interruptInFlight = false;
      if (button) button.classList.remove("cancelling");
    }, ESC_COUNT * ESC_INTERVAL_MS + 200);
  }

  // Tab-aware routing: each push from the backend carries the session_id
  // it came from. Non-active session pushes are dropped on the floor for
  // now (Phase 3 will buffer them per-tab). session_id is empty for app-
  // global pushes (theme, notes) — those always apply.
  let activeSessionId = "";
  function pushIsForActive(sid) {
    if (!sid) return true;          // app-global
    if (!activeSessionId) return true;  // no active yet — accept
    return sid === activeSessionId;
  }

  window.apiary = {
    setActiveSession(sid) {
      const nextSid = String(sid || "");
      if (nextSid === activeSessionId) return;
      const isFirstActivation = !activeSessionId;
      activeSessionId = nextSid;
      // On a real tab SWITCH we reset UI state so the outgoing tab's messages,
      // xterm scrollback, prompt banner, and sidebar don't leak into the new
      // tab. On the FIRST activation (app startup, empty "" → real sid), we
      // skip the reset because the backend's discovery thread might have
      // already pushed the active session's history before setActiveSession
      // arrives — clearing would wipe that history.
      if (isFirstActivation) return;
      try { clearMessages(); } catch (_) {}
      try { term.reset(); } catch (_) {}
      if (typeof hideThinkingBubble === "function") hideThinkingBubble(true);
      if (typeof hidePromptBanner === "function") hidePromptBanner();
      // Drop any active MCP prompt from the outgoing tab. The bridge will
      // time out and deny; a fresh request from the new tab will re-banner.
      mcpPrompt = null;
      try { allNotes = []; renderSidebar(); } catch (_) {}
      try { resetAgentsStripState(); } catch (_) {}
    },
    onMessage(msgJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const msg = typeof msgJson === "string" ? JSON.parse(msgJson) : msgJson;
        appendMessage(msg);
      } catch (e) {
        console.error("onMessage parse error", e);
      }
    },
    onMessages(arrJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) for (const m of arr) appendMessage(m);
      } catch (e) {
        console.error("onMessages parse error", e);
      }
    },
    onClear(sessionId) {
      if (!pushIsForActive(sessionId)) return;
      clearMessages();
    },
    onStatus(text, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      setStatus(text || "");
    },
    onToast(text, kind, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      toast(text, kind);
    },
    onPtyChunk(text, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        appendPtyChunk(String(text || ""));
      } catch (e) {
        console.error("onPtyChunk error", e);
      }
    },
    onPtyExit(code, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      toast(`Claude Code exited (code ${code}) — restart from menu`, "error");
    },
    onSessions(arrJson) {
      // Phase 2: data only. Phase 3 renders the tab bar.
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) {
          window.apiary.__sessions = arr;
          if (typeof window.renderTabs === "function") window.renderTabs(arr);
        }
      } catch (e) {
        console.error("onSessions parse error", e);
      }
    },
    onNotes(arrJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) {
          allNotes = arr;
          renderSidebar();
        }
      } catch (e) {
        console.error("onNotes parse error", e);
      }
    },
    onTheme(varsJson) {
      try {
        const vars = typeof varsJson === "string" ? JSON.parse(varsJson) : varsJson;
        if (vars && typeof vars === "object") {
          for (const [k, v] of Object.entries(vars)) {
            const name = k.startsWith("--") ? k : `--${k}`;
            document.documentElement.style.setProperty(name, v);
          }
        }
        // Re-apply pty colors to xterm (CSS vars alone don't reach the canvas).
        try {
          term.options.theme = {
            background: themeVar("--pty-bg", "#010409"),
            foreground: themeVar("--pty-fg", "#c9d1d9"),
          };
        } catch (_) { /* noop */ }
      } catch (e) {
        console.error("onTheme parse error", e);
      }
    },
    onHandoffBanner(count) {
      try { showHandoffBanner(count); } catch (e) { console.error("onHandoffBanner error", e); }
    },
    onUsage(payload) {
      try { renderUsage(payload); } catch (e) { console.error("onUsage error", e); }
    },
    onAgents(arrJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        renderAgentsStrip(Array.isArray(arr) ? arr : []);
      } catch (e) {
        console.error("onAgents parse error", e);
      }
    },
    onPermissionPrompt(pendingId, payloadJson) {
      // Structured permission request arriving from the MCP bridge. When
      // APIARY_PERMISSION_MCP=1 the TUI-scraping path does not fire for
      // permission prompts (claude routes through MCP); the scraper stays
      // compiled in as a fallback for the env-off case.
      //
      // Per-tab routing: payload.session_id tags the owning tab. Only
      // render the banner if that tab is the active one — otherwise the
      // Python side is already holding the prompt on the Session and
      // will re-push it when the user switches tabs, and the tab chip
      // gets a pending badge via renderTabs.
      try {
        const payload = typeof payloadJson === "string"
          ? JSON.parse(payloadJson) : payloadJson;
        const promptSid = (payload && payload.session_id) || "";
        if (promptSid && !pushIsForActive(promptSid)) return;
        // Drop any stale scraped prompt so the two paths don't stack.
        if (activePrompt) hidePromptBanner();
        mcpPrompt = { pendingId: String(pendingId || ""), payload: payload || {} };
        renderMcpPromptBanner(mcpPrompt.pendingId, mcpPrompt.payload);
      } catch (e) {
        console.error("onPermissionPrompt parse error", e);
      }
    },
  };

  // --- dev: Ctrl+R / F5 reloads the page (frontend hot-reload) --------------
  // WebView2 swallows the default reload shortcuts, so we wire them ourselves.
  // Reloads re-fetch index.html / app.css / app.js; the Python backend keeps
  // running, so tabs and pty subprocesses survive. Useful when iterating on
  // gui/web/* without restarting the whole GUI.
  document.addEventListener("keydown", (e) => {
    const isReload =
      e.key === "F5" ||
      ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r");
    if (isReload) {
      e.preventDefault();
      window.location.reload();
    }
  });

  // --- input wiring ---------------------------------------------------------
  inputEl.addEventListener("keydown", (e) => {
    if (!bridgeReady()) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // While a prompt banner is visible, forwarding the chat text would land
      // it inside whatever input field the pty prompt is showing (plan mode's
      // feedback field, etc.). Block the send and nudge the user.
      if (activePrompt) {
        toast("Pick an option in the banner above (or click ✕ to cancel the prompt).", "error");
        return;
      }
      const text = inputEl.value;
      inputEl.value = "";
      // Render optimistically — the tail will reconcile when the real record
      // lands. Skip empties and slash commands (slash commands shouldn't show
      // as user messages; claude often handles them differently). Also skip
      // the tentative render while we're in feedback-submission mode —
      // claude treats this as input to the plan prompt, not a new user msg.
      if (text && !text.startsWith("/") && !awaitingFeedback) {
        if (!waitingForAssistant) {
          // Fresh turn (not queued). Clear any stale .queued markers from
          // prior turns: if claude bundled multiple queued messages into one
          // reply (so not every queued msg got a dedicated response), those
          // stragglers still carry the class. Left uncleared, the next
          // assistant reply would wrongly insert before them.
          messagesEl.querySelectorAll("li.msg.user.queued")
            .forEach(el => el.classList.remove("queued"));
          startThinkingCounter();
        }
        // If waitingForAssistant is already true, claude is still working on
        // a prior turn — this message is queued, and needs the .queued marker
        // so the assistant reply for the prior turn lands before it.
        appendTentativeUserMessage(text, waitingForAssistant);
        // From here until the assistant message lands, the thinking bubble
        // may spawn on pty chunks. Slash commands and feedback submissions
        // don't get a normal assistant reply, so we don't arm for them.
        waitingForAssistant = true;
      }
      window.pywebview.api.send_input(text);
      if (awaitingFeedback) {
        awaitingFeedback = false;
        inputEl.placeholder = INPUT_PLACEHOLDER_DEFAULT;
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      interruptClaudeSession();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") {
      const sel = inputEl.value.substring(inputEl.selectionStart, inputEl.selectionEnd);
      if (!sel) {
        e.preventDefault();
        interruptClaudeSession();
      }
    }
  });

  // --- initial state --------------------------------------------------------
  setStatus("Waiting for session…");
  refreshTotalsBadges();

  // Hydrate persisted sidebar collapsed state once the bridge is ready.
  function hydrateCollapsed() {
    if (!bridgeReady()) {
      setTimeout(hydrateCollapsed, 100);
      return;
    }
    Promise.resolve(window.pywebview.api.get_sidebar_collapsed())
      .then((arr) => {
        if (Array.isArray(arr)) {
          collapsed = new Set(arr);
          renderSidebar();
        }
      })
      .catch((e) => console.error("get_sidebar_collapsed failed", e));
  }
  hydrateCollapsed();

  // --- chat-input resize via the pty-toggle bar ----------------------------
  // The bar above the composer (showing the "terminal" label + chevron)
  // doubles as the chat-input resize handle (T-2026-157). Drag anywhere on
  // the bar (except the bordered "terminal" label, which toggles the
  // terminal panel) to resize the textarea up/down. Persisted height is
  // applied to the textarea itself; the composer footer grows around it.
  const COMPOSER_MIN = 24;
  const COMPOSER_MAX_FRAC = 0.7;  // never let the input eat more than 70% of viewport

  function clampHeight(px) {
    const max = Math.max(COMPOSER_MIN, Math.floor(window.innerHeight * COMPOSER_MAX_FRAC));
    return Math.max(COMPOSER_MIN, Math.min(max, px));
  }

  function applyComposerHeight(px) {
    const h = clampHeight(px);
    inputEl.style.height = h + "px";
  }

  function hydrateComposerHeight() {
    if (!bridgeReady()) {
      setTimeout(hydrateComposerHeight, 100);
      return;
    }
    Promise.resolve(window.pywebview.api.get_composer_height())
      .then((px) => {
        if (typeof px === "number" && px > 0) applyComposerHeight(px);
      })
      .catch((e) => console.error("get_composer_height failed", e));
  }
  hydrateComposerHeight();

  let dragState = null;
  ptyToggleEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragState = { startY: e.clientY, startHeight: inputEl.offsetHeight };
    ptyToggleEl.classList.add("dragging");
    document.body.style.cursor = "ns-resize";
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    // Drag up (clientY decreases) -> taller input. Subtract delta so up-drag
    // gives a positive growth.
    const delta = dragState.startY - e.clientY;
    applyComposerHeight(dragState.startHeight + delta);
  });

  document.addEventListener("mouseup", () => {
    if (!dragState) return;
    const finalHeight = inputEl.offsetHeight;
    dragState = null;
    ptyToggleEl.classList.remove("dragging");
    document.body.style.cursor = "";
    if (bridgeReady()) {
      Promise.resolve(window.pywebview.api.save_composer_height(finalHeight))
        .catch((e) => console.error("save_composer_height failed", e));
    }
  });
})();
