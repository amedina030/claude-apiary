/* file_drop.js — drag-and-drop file attachments for the composer.
 *
 * Drops are handled natively in Python: pywebview hands the host the real
 * absolute path of each dropped file (pywebviewFullPath), so App._on_file_drop
 * records a *reference* (no copy) and pushes the updated list here via
 * setFiles(). This file owns only the UI: the drag overlay, the chip panel
 * above the composer, and the manifest appended to the outgoing prompt so
 * Claude can Read the referenced paths.
 *
 * Exposes window.apiaryFileDrop = { setFiles, manifest, markAnnounced,
 *                                   hasFiles, count, clearAll }.
 */
(function () {
  "use strict";

  // Reference descriptors from Python: {id, name, path, type, size, added, exists}.
  // We add a local `announced` flag (sent-to-claude-already) that Python doesn't
  // track, preserved across setFiles() pushes via this id→announced map.
  let files = [];
  const announcedById = new Map();

  // Outline SVG glyphs (currentColor) — matches the rest of the GUI chrome.
  const ICON_IMAGE =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="2" y="3" width="12" height="10" rx="1.5"/>' +
    '<circle cx="5.6" cy="6.6" r="1.1"/>' +
    '<path d="M3 11.5l3.2-2.8 2.3 1.9 2-1.6 2.5 2.8"/></svg>';
  const ICON_FILE =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M4 2.5h5L12 5.5V13.5H4z"/><path d="M9 2.5V6h3"/></svg>';

  let overlayEl = null;
  let rowsEl = null;   // #refs-rows — the scrollable body
  let titleEl = null;  // #refs-title — "referenced files · N"
  let clearBtnEl = null; // #refs-clear
  // dragenter/dragleave fire per child element; count depth so the overlay only
  // hides when the cursor truly leaves the window.
  let dragDepth = 0;

  function bridgeReady() {
    return !!(window.pywebview && window.pywebview.api);
  }

  function isFileDrag(e) {
    const dt = e.dataTransfer;
    if (!dt) return false;
    return Array.from(dt.types || []).indexOf("Files") !== -1;
  }

  function renderPanel() {
    if (!rowsEl) return;
    rowsEl.innerHTML = "";
    files.forEach((f) => {
      const row = document.createElement("span");
      let cls = "refs-row";
      if (f.announced) cls += " announced"; // already shared with claude → dim
      if (f.exists === false) cls += " missing"; // target moved/deleted since add
      row.className = cls;

      const icon = document.createElement("span");
      icon.className = "refs-row-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = (f.type || "").indexOf("image/") === 0 ? ICON_IMAGE : ICON_FILE;

      // Path only (no size/time). CSS truncates it from the left so the
      // filename tail stays readable in the narrow sidebar column.
      const path = document.createElement("span");
      path.className = "refs-row-path";
      path.textContent = f.path;

      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "refs-row-remove";
      rm.setAttribute("aria-label", "remove " + f.name);
      rm.textContent = "✕";
      rm.addEventListener("click", () => removeFile(f.id));

      row.appendChild(icon);
      row.appendChild(path);
      row.appendChild(rm);
      rowsEl.appendChild(row);
    });
    // Header count + clear button appear only when there's something staged;
    // the bare "referenced files" header always remains to mark the spot.
    if (titleEl) {
      titleEl.textContent =
        files.length > 0 ? "referenced files · " + files.length : "referenced files";
    }
    if (clearBtnEl) clearBtnEl.classList.toggle("hidden", files.length === 0);
  }

  // Adopt a list pushed from Python, preserving each file's announced flag.
  function setFiles(list) {
    const incoming = Array.isArray(list) ? list : [];
    files = incoming.map((f) => ({
      ...f,
      announced: announcedById.get(f.id) || false,
    }));
    // Forget announced flags for ids no longer present.
    const live = new Set(files.map((f) => f.id));
    Array.from(announcedById.keys()).forEach((id) => {
      if (!live.has(id)) announcedById.delete(id);
    });
    renderPanel();
  }

  function removeFile(id) {
    announcedById.delete(id);
    if (bridgeReady()) {
      Promise.resolve(window.pywebview.api.remove_file(id))
        .then((list) => setFiles(list))
        .catch(() => {});
    } else {
      files = files.filter((f) => f.id !== id);
      renderPanel();
    }
  }

  // --- drag overlay (visual only — the drop itself is handled in Python) -----

  function showOverlay() { if (overlayEl) overlayEl.classList.remove("hidden"); }
  function hideOverlay() { if (overlayEl) overlayEl.classList.add("hidden"); }

  function wireDragOverlay() {
    window.addEventListener("dragenter", (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragDepth += 1;
      showOverlay();
    }, true);

    window.addEventListener("dragover", (e) => {
      if (!isFileDrag(e)) return;
      // Required for the drop event to fire (and to stop the webview from
      // navigating to the file). The actual file capture is pywebview's.
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    }, true);

    window.addEventListener("dragleave", (e) => {
      if (!isFileDrag(e)) return;
      dragDepth -= 1;
      if (dragDepth <= 0) { dragDepth = 0; hideOverlay(); }
    }, true);

    window.addEventListener("drop", (e) => {
      if (!isFileDrag(e)) return;
      // Don't stopPropagation — pywebview's own drop listener must still run to
      // capture the paths. Just reset the overlay; Python pushes the new list.
      dragDepth = 0;
      hideOverlay();
    }, true);
  }

  function init() {
    overlayEl = document.getElementById("drop-overlay");
    rowsEl = document.getElementById("refs-rows");
    titleEl = document.getElementById("refs-title");
    clearBtnEl = document.getElementById("refs-clear");
    if (clearBtnEl) {
      clearBtnEl.addEventListener("click", () => window.apiaryFileDrop.clearAll());
    }
    wireDragOverlay();
    // Re-render any references already recorded this run (e.g. after a reload).
    if (bridgeReady()) {
      Promise.resolve(window.pywebview.api.list_files())
        .then((list) => setFiles(list))
        .catch(() => {});
    } else {
      renderPanel();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.apiaryFileDrop = {
    setFiles,
    hasFiles() { return files.length > 0; },
    count() { return files.length; },
    clearAll() {
      announcedById.clear();
      if (bridgeReady()) {
        try { window.pywebview.api.clear_files(); } catch (_) {}
      }
      files = [];
      renderPanel();
    },
    // Manifest block for files claude hasn't been told about yet. Already-shared
    // ones are skipped — they're in the conversation history. Empty string when
    // there's nothing new, so app.js can append unconditionally.
    manifest() {
      const fresh = files.filter((f) => !f.announced && f.exists !== false);
      if (fresh.length === 0) return "";
      const lines = fresh.map((f) => "- " + f.path + " (" + f.type + ")");
      return "[attached files — read these with the Read tool:]\n" + lines.join("\n");
    },
    // Mark every referenced file as shared after a send, so its path isn't
    // re-listed next turn.
    markAnnounced() {
      let changed = false;
      files.forEach((f) => {
        if (!f.announced) {
          f.announced = true;
          announcedById.set(f.id, true);
          changed = true;
        }
      });
      if (changed) renderPanel();
    },
  };
})();
