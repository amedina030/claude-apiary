/* file_drop.js — drag-and-drop file attachments for the composer.
 *
 * Drops are handled natively in Python: pywebview hands the host the real
 * absolute path of each dropped file (pywebviewFullPath), so App._on_file_drop
 * records a *reference* (no copy) and pushes the updated list here via
 * setFiles(). This file owns only the UI: the drag overlay, the chip panel
 * above the composer. The outgoing attach-manifest is NOT built here — it's
 * assembled authoritatively in Python at send time (Api.manifest_and_mark),
 * which re-checks each path's existence the moment the user sends so a file
 * that vanished after being dropped is never shipped as a dead path. This
 * file owns only the UI; `announced` (shared-with-claude) is now a Python-
 * owned field that arrives on each descriptor.
 *
 * Exposes window.apiaryFileDrop = { setFiles, hasFiles, count, clearAll }.
 */
(function () {
  "use strict";

  // Reference descriptors from Python: {id, name, path, type, size, added,
  // exists, announced}. `announced` (already shared with claude) is owned by
  // Python now — it's set when manifest_and_mark ships a path — so the panel
  // just reflects whatever the latest push says.
  let files = [];

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
  let paneEl = null;   // #refs-pane — carries .collapsed / .has-files
  let toggleEl = null; // #refs-toggle — the title-as-collapse-button
  // User-driven collapse of the rows body. Sticky while files are staged;
  // reset to expanded once the pane empties (see renderPanel).
  let collapsed = false;
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
    const has = files.length > 0;
    if (titleEl) {
      titleEl.textContent = has ? "referenced files · " + files.length : "referenced files";
    }
    if (clearBtnEl) clearBtnEl.classList.toggle("hidden", !has);
    // Collapse only makes sense with files staged; an empty pane resets to
    // expanded so the next drop is visible without a click.
    if (!has) collapsed = false;
    if (paneEl) paneEl.classList.toggle("has-files", has);
    applyCollapsed();
  }

  // Reflect the collapse state on the pane + the toggle's aria.
  function applyCollapsed() {
    if (paneEl) paneEl.classList.toggle("collapsed", collapsed);
    if (toggleEl) toggleEl.setAttribute("aria-expanded", String(!collapsed));
  }

  // Adopt a list pushed from Python. `announced` rides along on each
  // descriptor (Python-owned), so there's nothing local to reconcile.
  function setFiles(list) {
    const incoming = Array.isArray(list) ? list : [];
    files = incoming.map((f) => ({ ...f, announced: !!f.announced }));
    renderPanel();
  }

  function removeFile(id) {
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
    paneEl = document.getElementById("refs-pane");
    toggleEl = document.getElementById("refs-toggle");
    if (clearBtnEl) {
      clearBtnEl.addEventListener("click", () => window.apiaryFileDrop.clearAll());
    }
    if (toggleEl) {
      toggleEl.addEventListener("click", () => {
        if (files.length === 0) return; // nothing to collapse on an empty pane
        collapsed = !collapsed;
        applyCollapsed();
      });
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
      if (bridgeReady()) {
        try { window.pywebview.api.clear_files(); } catch (_) {}
      }
      files = [];
      renderPanel();
    },
  };
})();
