// Claude Code interactive-prompt parser — extracted from app.js so it can be
// unit-tested in Node without a full browser / xterm.js context.
//
// The parser operates on a plain `string[]` (xterm's rendered-and-trimmed
// buffer rows). Producing that array from raw pty bytes needs cursor-movement
// emulation (xterm does that live in the browser). Tests feed hand-crafted
// line arrays that mirror the buffer shapes we've observed in the wild.
//
// Exposed in both environments:
//   - Browser: attaches to `window.apiaryPromptDetector`
//   - Node:    `module.exports = { detectPrompt, ... }`

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.apiaryPromptDetector = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  const PROMPT_MAX_OPTIONS = 9;
  const PROMPT_SCAN_BACK_LINES = 6;
  // How far above a navigation footer we'll hunt for the menu's first option in
  // the glyph-less fallback pass.
  const FOOTER_LOOKBACK_LINES = 30;
  // claude prints this under arrow-navigable menus. It's the strongest
  // menu-specific signal we have, so it gates the looser detection paths
  // (per-option descriptions, and the glyph-less shape) against false positives.
  const NAV_HINT = /↑|↓|to select|to navigate|esc to cancel|enter to (?:select|confirm)/i;

  // Parse a numbered-option block whose first (lowest-numbered) option sits at
  // line `i`. The selector glyph is optional: claude historically rendered ❯
  // (U+276F), 2.1.116+ emits a literal '>', and the AskUserQuestion card emits
  // NO glyph at all (the selected row is shown via xterm cell highlight, which
  // translateToString() discards). Returns {options, lastIdx, sawDescription}
  // or null when fewer than two options parse.
  function parseOptionsFrom(lines, i) {
    // Capture the prefix (group 1) so we know the column the option number sits
    // at — description lines indent past it.
    const sel = lines[i].match(/^(\s*[❯>]?\s*)(\d+)\.\s+(.+)$/);
    if (!sel) return null;
    const firstNum = parseInt(sel[2], 10);
    const hasGlyph = /[❯>]/.test(sel[1]);
    const options = [{
      number: firstNum,
      text: sel[3].trim(),
      description: "",
      // We can only know the highlighted row from the glyph; the glyph-less
      // shape carries the selection in cell attributes the string buffer drops,
      // so leave it unselected (Phase 2 reads attributes). Clicking drives via
      // the option number regardless, so this is cosmetic.
      selected: hasGlyph,
      indent: sel[1].length,
    }];
    let expected = firstNum + 1;
    let sawDescription = false;
    let lastIdx = i;
    for (let j = i + 1; j < lines.length && options.length < PROMPT_MAX_OPTIONS; j++) {
      lastIdx = j;
      const m = lines[j].match(/^(\s+)(\d+)\.\s+(.+)$/);
      if (m && parseInt(m[2], 10) === expected) {
        options.push({
          number: expected,
          text: m[3].trim(),
          description: "",
          selected: false,
          indent: m[1].length,
        });
        expected += 1;
        continue;
      }
      if (m) break;                       // numbered, but out of sequence
      if (lines[j].trim() === "") continue;
      // Non-blank, non-numbered line. AskUserQuestion and other rich menus
      // render an indented description / wrapped sub-line under each option;
      // the old parser stopped dead here and lost the whole prompt. Attach it
      // to the current option when it indents past that option's number
      // column; anything at-or-left of the number (footer, chrome, ordinary
      // output) ends the option list.
      const lead = lines[j].match(/^(\s*)/)[1].length;
      const last = options[options.length - 1];
      if (lead > last.indent) {
        const extra = lines[j].trim();
        last.description = last.description ? last.description + " " + extra : extra;
        sawDescription = true;
        continue;
      }
      break;
    }
    if (options.length < 2) return null;
    return { options, lastIdx, sawDescription };
  }

  // Does any line in [from, to) carry the navigation footer?
  function hasNavFooter(lines, from, to) {
    for (let k = Math.max(0, from); k < Math.min(lines.length, to); k++) {
      if (NAV_HINT.test(lines[k])) return true;
    }
    return false;
  }

  // Locate the first option line of a glyph-less menu: when claude omits the
  // selector glyph entirely, the only reliable anchor is the navigation footer,
  // so we find it and walk up to the menu's "1." line. Returns the anchor index
  // or -1.
  function findGlyphlessAnchor(lines) {
    for (let f = lines.length - 1; f >= 0; f--) {
      if (!NAV_HINT.test(lines[f])) continue;
      for (let k = f - 1; k >= 0 && k >= f - FOOTER_LOOKBACK_LINES; k--) {
        if (/^\s*1\.\s+\S/.test(lines[k])) return k;
      }
    }
    return -1;
  }

  function buildResult(lines, i, parsed) {
    const { options, lastIdx } = parsed;
    let question = "";
    let questionIdx = -1;
    for (let k = i - 1; k >= 0 && k >= i - PROMPT_SCAN_BACK_LINES; k--) {
      const t = lines[k].trim();
      if (t) { question = t; questionIdx = k; break; }
    }
    let context = "";
    if (questionIdx > 0) {
      const PLAN_DIV = /^\s*╌{10,}\s*$/;
      const CHROME_LINE = /^\s*─{10,}\s*$|^\s*[─❯⏸▐▛▜▟▝▘█]/;
      let bottomDiv = -1;
      for (let k = questionIdx - 1; k >= 0; k--) {
        if (PLAN_DIV.test(lines[k])) { bottomDiv = k; break; }
      }
      if (bottomDiv >= 0) {
        let topBound = 0;
        for (let k = bottomDiv - 1; k >= 0; k--) {
          if (PLAN_DIV.test(lines[k]) || CHROME_LINE.test(lines[k])) {
            topBound = k + 1;
            break;
          }
        }
        const body = lines.slice(topBound, bottomDiv)
                          .map(s => s.replace(/^\s{1,2}/, ""))
                          .join("\n")
                          .replace(/\n{3,}/g, "\n\n")
                          .trim();
        if (body) context = body;
      }
    }
    void lastIdx;
    const signature = (context ? context.slice(0, 80) + "|" : "") +
                      options.map(o => `${o.number}.${o.text}`).join("|");
    return { question, context, options, signature };
  }

  function detectPrompt(lines) {
    // Pass 1 — glyphed anchor (❯ or '>'), scanned bottom-up. This is the
    // historical path; the glyph uniquely marks the selected (top) option, so
    // it's a safe anchor with no footer needed for classic shapes.
    for (let i = lines.length - 1; i >= 0; i--) {
      if (!/^\s*[❯>]\s*\d+\.\s+/.test(lines[i])) continue;
      const parsed = parseOptionsFrom(lines, i);
      if (!parsed) continue;
      // Guard the looser description path against false positives: a menu that
      // carries per-option descriptions must also show a navigation footer.
      // Classic description-less numbered prompts keep the old, footer-free
      // acceptance so plan-mode / trust-folder / permission shapes don't
      // regress.
      if (parsed.sawDescription &&
          !hasNavFooter(lines, i, parsed.lastIdx + 3)) {
        continue;
      }
      return buildResult(lines, i, parsed);
    }

    // Pass 2 — glyph-less menu (AskUserQuestion). No glyph means no per-row
    // anchor, so we key off the navigation footer (a strong menu-specific
    // signal) and walk up to the "1." line. Always footer-gated, so ordinary
    // numbered output can't masquerade as a menu.
    const anchor = findGlyphlessAnchor(lines);
    if (anchor >= 0) {
      const parsed = parseOptionsFrom(lines, anchor);
      if (parsed) return buildResult(lines, anchor, parsed);
    }
    return null;
  }

  return { detectPrompt, PROMPT_MAX_OPTIONS, PROMPT_SCAN_BACK_LINES };
});
