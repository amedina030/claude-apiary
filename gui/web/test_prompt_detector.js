// Unit tests for prompt_detector.js. Run with `node test_prompt_detector.js`
// (no npm dependencies). The detector operates on the rendered-and-trimmed
// xterm buffer as a string[]; these fixtures model the shapes we've observed.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  detectPrompt,
  planArrowSteps,
  highlightedOptionIndex,
} = require("./prompt_detector");

// --- helpers --------------------------------------------------------------

function linesOf(text) {
  // Literal block → string[] with trailing-whitespace trim (matches what
  // xterm's translateToString(true).replace(/\s+$/, "") produces).
  return text.split("\n").map((s) => s.replace(/\s+$/, ""));
}

// --- fixtures -------------------------------------------------------------

// Trust folder (2 options). Claude Code places the question directly above
// the options, with no interstitial context line — matches the capture that
// T-2026-153 notes "parses correctly".
const TRUST_FOLDER = linesOf(`
Do you trust the files in this folder?

❯ 1. Yes, proceed
  2. No, exit

`);

// Tool-permission Bash (3 options). NOTE: The real prompt may include the
// command on an intermediate line between question and options; the detector
// currently picks "last non-empty line above options" as the question, so a
// real capture is needed to settle the shape. Tracked as a follow-up.
// This fixture models the simple shape (question directly above options).
const TOOL_PERMISSION_BASH = linesOf(`
Allow Bash command?

❯ 1. Yes
  2. Yes, and don't ask again for this command
  3. No, and tell Claude what to do differently

`);

// Plan-mode approval (4 options). The plan body is framed by ╌-dividers; the
// detector should extract it into the context field.
const PLAN_MODE_APPROVAL = linesOf(`
────────────────────────────────────────
⏸ plan mode on

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  ## Goal
  Rewrite the auth middleware to stop
  storing session tokens in plaintext.

  ## Steps
  1. Move tokens to encrypted store
  2. Update login handler
  3. Add migration
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

Would you like to proceed?

❯ 1. Yes, proceed
  2. Yes, and auto-accept edits
  3. No, keep planning
  4. Tell Claude what to change with this feedback

`);

// Same shape as TOOL_PERMISSION_BASH but with the literal '>' selector
// claude-code 2.1.116+ emits instead of ❯. This is the .claude/ protect-self
// gate prompt observed during the L-2026-111 investigation.
const PROTECT_SELF_GATE = linesOf(`
Do you want to create test.txt?

>  1. Yes
   2. Yes, and allow Claude to edit its own settings for this session
   3. No

`);

// Single ❯ with no second option → should NOT match (needs >=2 options).
const NOT_A_PROMPT_SINGLE = linesOf(`
Some status text

❯ 1. lonely option

`);

// Options out of order (1, 3, 4) → only option 1 matches; bails out.
const OUT_OF_ORDER = linesOf(`
What now?

❯ 1. First
  3. Third
  4. Fourth

`);

// No ❯ marker at all — buffer with arbitrary chatter.
const NO_MARKER = linesOf(`
random
text without any numbered prompt
hello
`);

// --- tests ----------------------------------------------------------------

test("trust-folder: 2 options, no plan body", () => {
  const r = detectPrompt(TRUST_FOLDER);
  assert.ok(r, "should detect a prompt");
  assert.equal(r.question, "Do you trust the files in this folder?");
  assert.equal(r.options.length, 2);
  assert.deepEqual(
    r.options.map((o) => [o.number, o.text, o.selected]),
    [
      [1, "Yes, proceed", true],
      [2, "No, exit", false],
    ]
  );
  assert.equal(r.context, "", "no ╌-framed body → empty context");
});

test("tool-permission: 3 options, question present, no context", () => {
  const r = detectPrompt(TOOL_PERMISSION_BASH);
  assert.ok(r);
  assert.equal(r.question, "Allow Bash command?");
  assert.equal(r.options.length, 3);
  assert.equal(r.options[0].selected, true);
  assert.equal(r.options[2].text, "No, and tell Claude what to do differently");
  assert.equal(r.context, "");
});

test("plan-mode: 4 options plus extracted plan body in context", () => {
  const r = detectPrompt(PLAN_MODE_APPROVAL);
  assert.ok(r);
  assert.equal(r.question, "Would you like to proceed?");
  assert.equal(r.options.length, 4);
  assert.match(r.context, /Rewrite the auth middleware/);
  assert.match(r.context, /## Steps/);
  assert.ok(
    !r.context.includes("⏸ plan mode on"),
    "chrome above the top ╌-divider should not leak into context"
  );
});

test("protect-self gate: '>' selector (2.1.116+) parses same as ❯", () => {
  const r = detectPrompt(PROTECT_SELF_GATE);
  assert.ok(r, "should detect a prompt with '>' selector");
  assert.equal(r.question, "Do you want to create test.txt?");
  assert.equal(r.options.length, 3);
  assert.equal(r.options[0].selected, true);
  assert.equal(r.options[0].text, "Yes");
  assert.equal(
    r.options[1].text,
    "Yes, and allow Claude to edit its own settings for this session"
  );
  assert.equal(r.options[2].text, "No");
});

test("single-option block is not treated as a prompt", () => {
  const r = detectPrompt(NOT_A_PROMPT_SINGLE);
  assert.equal(r, null);
});

test("out-of-order numbering stops at the first gap", () => {
  const r = detectPrompt(OUT_OF_ORDER);
  // Only option 1 was parsed; 3+4 violate the sequence → under the 2-option
  // minimum → returns null.
  assert.equal(r, null);
});

test("buffer without a ❯ marker returns null", () => {
  const r = detectPrompt(NO_MARKER);
  assert.equal(r, null);
});

test("signature is stable across calls on the same fixture", () => {
  const a = detectPrompt(PLAN_MODE_APPROVAL);
  const b = detectPrompt(PLAN_MODE_APPROVAL);
  assert.equal(a.signature, b.signature);
});

test("signature changes when options differ", () => {
  const a = detectPrompt(TRUST_FOLDER);
  const b = detectPrompt(TOOL_PERMISSION_BASH);
  assert.notEqual(a.signature, b.signature);
});

// AskUserQuestion card (REAL shape, from a GUI capture). Crucially, claude
// emits NO selector glyph here — every option sits at the same column with a
// single leading space, and the selected row is shown via xterm cell highlight
// that translateToString() discards. Each option carries an indented
// description sub-line; the card auto-appends a "Type something" (Other) entry
// and a "Chat about this" entry, and prints a navigation footer. The Phase 1
// parser anchored on the glyph and so never matched this at all.
const ASKUSERQUESTION_CARD = linesOf(`
Repro test: this very prompt is a deliberate AskUserQuestion. How did it just appear in your GUI?

 1. Terminal card, arrow keys
    It showed up in the terminal pane as an arrow-key selection card.
 2. Clickable banner
    It appeared as clickable buttons in the chat pane.
 3. Both / something else
    It showed up some other way, or behaved oddly.
 4. Type something.

 5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
`);

// Same glyph-less option/description shape but with NO navigation footer — must
// be rejected, so ordinary indented numbered output can't masquerade as a menu.
const DESCRIPTIONS_NO_FOOTER = linesOf(`
Some heading

 1. Option one
    a description line
 2. Option two
    another description
`);

test("AskUserQuestion card: parses all options with descriptions attached", () => {
  const r = detectPrompt(ASKUSERQUESTION_CARD);
  assert.ok(r, "should detect the card");
  assert.match(r.question, /How did it just appear in your GUI\?$/);
  assert.equal(r.options.length, 5, "1-3 plus Type-something and Chat-about-this");
  // Glyph-less shape: selection lives in cell attributes the buffer drops, so
  // no option is marked selected (clicking drives by number regardless).
  assert.equal(r.options[0].selected, false);
  assert.equal(r.options[0].text, "Terminal card, arrow keys");
  assert.match(r.options[0].description, /arrow-key selection card/);
  assert.match(r.options[1].description, /clickable buttons/);
  assert.equal(r.options[3].text, "Type something.");
  assert.equal(r.options[3].description, "", "no sub-line → empty description");
  assert.equal(r.options[4].text, "Chat about this");
});

test("descriptions without a nav footer are rejected (false-positive guard)", () => {
  const r = detectPrompt(DESCRIPTIONS_NO_FOOTER);
  assert.equal(r, null);
});

// Real failure captured live (#H-2026-241 verify): a glyphed AskUserQuestion
// whose per-option descriptions are long enough that xterm HARD-WRAPS each tail
// onto a fresh row at column 0 (no re-indent). The Phase-2 parser stopped at the
// first wrapped row → parsed a single option → returned null → the GUI showed
// the "couldn't parse — check the terminal" fallback instead of the banner.
const ASKUSERQUESTION_WRAPPED = linesOf(`
This is a live test of the 2a highlight detection. When this card appeared in
the banner, did the option the cursor is on show an accent-colored border?

> 1. Border present
     The highlighted option has a visible accent-colored border in the
banner so 2a is working.
  2. No border
     No option in the banner has a highlight or border so 2a is NOT
working (regression or detection miss).
  3. No banner at all
     No prompt banner appeared in the GUI for this question.
  4. Type something.

  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
`);

test("wrapped descriptions: column-0 overflow folds back, all options parse", () => {
  const r = detectPrompt(ASKUSERQUESTION_WRAPPED);
  assert.ok(r, "must parse despite hard-wrapped descriptions");
  assert.equal(r.options.length, 5, "1-3 plus Type-something and Chat-about-this");
  assert.equal(r.options[0].selected, true, "glyph marks option 1");
  // The wrapped tail is folded back into the same description, not dropped.
  assert.match(r.options[0].description, /accent-colored border in the banner so 2a is working\.$/);
  assert.match(r.options[1].description, /2a is NOT working \(regression or detection miss\)\.$/);
  assert.equal(r.options[3].text, "Type something.");
  assert.equal(r.options[3].description, "", "no description → nothing folded");
  assert.equal(r.options[4].text, "Chat about this");
});

// Real failure captured live (#H-2026-242 re-verify, 2026-06-06): the actual
// AskUserQuestion card renders its option NUMBERS flush at column 0 (no leading
// space), with each description indented under them and wrapping with a hanging
// indent. The earlier fixtures used a 1-space option indent, so they never hit
// the `\s+` subsequent-option matcher that rejected column-0 numbers — every
// option after the first was dropped (and the b910219 fold-back then swallowed
// them into option 1's description) → single option → null → fallback toast.
const ASKUSERQUESTION_COL0 = linesOf(`
This is the Phase 2 re-verify test — does the prompt banner render this card?

1. Renders perfectly
   The card shows up in the banner with a visible accent border on this first
   option, all five options are listed in full, and there is no fallback toast.
2. Card shows, option 1 not highlighted
   The banner card renders and all options are present, but the accent border is
   missing or sitting on the wrong option.
3. Truncated option list
   The banner appears but is missing some options or cuts off mid-description.
4. Type something.

5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
`);

test("column-0 card: option numbers flush at col 0 still parse every option", () => {
  const r = detectPrompt(ASKUSERQUESTION_COL0);
  assert.ok(r, "must detect a card whose option numbers sit at column 0");
  assert.equal(r.options.length, 5, "1-3 plus Type-something and Chat-about-this");
  assert.equal(r.options[0].text, "Renders perfectly");
  assert.match(r.options[0].description, /no fallback toast\.$/);
  assert.equal(r.options[1].text, "Card shows, option 1 not highlighted");
  assert.match(r.options[1].description, /wrong option\.$/);
  assert.equal(r.options[2].text, "Truncated option list");
  assert.equal(r.options[3].text, "Type something.");
  assert.equal(r.options[3].description, "");
  assert.equal(r.options[4].text, "Chat about this");
});

// A description-less option followed by column-0 TUI chrome (the input
// composer) must NOT have that chrome folded into it — the fold only extends an
// already-started description that directly abuts its tail.
const CLASSIC_THEN_CHROME = linesOf(`
Do you want to proceed?
❯ 1. Yes
  2. No
│ > type your message here                       │
`);

test("column-0 chrome after a description-less option is not absorbed", () => {
  const r = detectPrompt(CLASSIC_THEN_CHROME);
  assert.ok(r);
  assert.equal(r.options.length, 2);
  assert.equal(r.options[1].text, "No");
  assert.equal(r.options[1].description, "", "input-box chrome must not leak in");
});

// Glyph-less numbered output WITHOUT a footer must not be mistaken for a menu —
// this is the ordinary-output false-positive the footer gate exists to stop.
const GLYPHLESS_NO_FOOTER = linesOf(`
Here are the steps I'll take:

 1. Read the file
 2. Edit the function
 3. Run the tests
`);

test("glyph-less numbered output without a footer is not a prompt", () => {
  assert.equal(detectPrompt(GLYPHLESS_NO_FOOTER), null);
});

// Glyph-less menu WITH a footer but no per-option descriptions (compact
// AskUserQuestion). Must still be detected via the footer-anchored pass.
const GLYPHLESS_COMPACT = linesOf(`
Which one?

 1. First choice
 2. Second choice
 3. Third choice
Enter to select · ↑/↓ to navigate · Esc to cancel
`);

test("glyph-less compact menu (footer, no descriptions) is detected", () => {
  const r = detectPrompt(GLYPHLESS_COMPACT);
  assert.ok(r, "footer-anchored pass should catch it");
  assert.equal(r.question, "Which one?");
  assert.equal(r.options.length, 3);
  assert.equal(r.options[2].text, "Third choice");
});

// Phase 2: cell-attribute highlight hint. GLYPHLESS_COMPACT option lines are at
// buffer indices 3,4,5 (leading blank line shifts everything by one). The
// browser passes the highlighted row index derived from xterm cell attributes;
// here row 4 = the second option.
test("highlight hint marks the cursor row on a glyph-less menu", () => {
  const r = detectPrompt(GLYPHLESS_COMPACT, { highlightedRows: [4] });
  assert.ok(r);
  assert.deepEqual(
    r.options.map((o) => o.selected),
    [false, true, false],
    "only the highlighted row (option 2) is selected"
  );
  // lineIdx is exposed so the arrow driver can re-verify after stepping.
  assert.deepEqual(r.options.map((o) => o.lineIdx), [3, 4, 5]);
});

test("highlight hint accepts a Set and survives a non-option index", () => {
  // Index 6 is the footer, not an option — must be ignored, not crash.
  const r = detectPrompt(GLYPHLESS_COMPACT, { highlightedRows: new Set([5, 6]) });
  assert.ok(r);
  assert.deepEqual(r.options.map((o) => o.selected), [false, false, true]);
});

test("glyph-less menu without a highlight hint leaves all rows unselected", () => {
  // Back-compat: omitting the hint preserves pre-Phase-2 behavior.
  const r = detectPrompt(GLYPHLESS_COMPACT);
  assert.deepEqual(r.options.map((o) => o.selected), [false, false, false]);
});

test("glyph selection still wins when no highlight hint is supplied", () => {
  const r = detectPrompt(TRUST_FOLDER);
  assert.deepEqual(r.options.map((o) => o.selected), [true, false]);
});

// Regression: the claude TUI renders the assistant's own chat messages into the
// same xterm buffer the detector scrapes. A numbered list in prose, with a
// nav-hint footer rendered well below it (claude's input hint), must NOT be
// mistaken for a menu. The glyph-less pass walks up to 30 lines from a footer to
// find a "1.", so without a proximity gate this prose list false-positives.
const PROSE_LIST_FAR_FOOTER = linesOf(`
Check, in order:
 1. Banner appears with every option
 2. Terminal panel stays put
 3. Click an option to commit

Some more prose explaining the next steps.
Another line that is not a menu at all.

Enter to select · ↑/↓ to navigate · Esc to cancel
`);

test("prose numbered list with a far-below footer is not a menu", () => {
  assert.equal(detectPrompt(PROSE_LIST_FAR_FOOTER), null);
});

// --- false-positive hardening (stress battery, 2026-06-06) -----------------
// Found by throwing realistic chat-buffer shapes at the detector: a numbered
// list in claude's OWN prose, sitting near anything footer-like, was being
// mis-read as a selectable menu. Two distinct leaks, both now closed.

// Leak 1: the footer gate matched any line that merely *mentioned* a nav phrase
// ("to select"), so claude explaining a menu in chat turned the list above it
// into a phantom menu. The gate now demands a footer SHAPE (arrow glyphs, or
// >=2 distinct nav signals), not a stray phrase in a sentence.
const FOOTER_PHRASE_IN_PROSE = linesOf(`
Steps:
1. Open the file
2. Make the edit
The footer reads "Enter to select" at the bottom.
`);

test("a nav phrase embedded in a prose sentence is not a footer", () => {
  assert.equal(detectPrompt(FOOTER_PHRASE_IN_PROSE), null);
});

// Leak 2: the proximity gate scanned a fixed 2-line window, so a single line of
// prose between the list and a real footer still slipped through. The gate now
// requires the footer to be the FIRST non-blank line after the options.
const PROSE_LINE_THEN_FOOTER = linesOf(`
I'll proceed with:
1. step one
2. step two
3. step three
Done. Let me know if that works.
Enter to select · ↑/↓ to navigate · Esc to cancel
and more chatter after.
`);

test("one prose line between the list and a footer is not a menu", () => {
  assert.equal(detectPrompt(PROSE_LINE_THEN_FOOTER), null);
});

// A markdown blockquote ordered list (claude quoting docs). The '>' begins each
// line, so only the first option anchors pass 1; the continuation lines keep
// their '>' and never match the glyph-less subsequent-option matcher → bails.
const BLOCKQUOTE_LIST = linesOf(`
As the docs say:
> 1. First do this
> 2. Then do that
> 3. Finally this
`);

test("markdown blockquote ordered list is not a menu", () => {
  assert.equal(detectPrompt(BLOCKQUOTE_LIST), null);
});

// KNOWN LIMITATION (do not "fix" by loosening real-menu detection): a bare
// numbered list immediately followed by the EXACT navigation footer is
// byte-for-byte identical to a real compact menu (see GLYPHLESS_COMPACT) — there
// is no scrape-level signal that distinguishes claude QUOTING the footer from
// the TUI PRINTING it. This is the architectural reason the AskUserQuestion
// banner is sourced from the transcript tool_use record, not the scrape; the
// scrape path here is only a fallback for non-AskUserQuestion menus. Asserting
// the current (unavoidable) behavior so a future change to it is deliberate.
const BARE_LIST_EXACT_FOOTER = linesOf(`
The compact menu looks like this:
1. First choice
2. Second choice
3. Third choice
Enter to select · ↑/↓ to navigate · Esc to cancel
`);

test("bare list + exact footer is indistinguishable from a real menu (known)", () => {
  const r = detectPrompt(BARE_LIST_EXACT_FOOTER);
  assert.ok(r, "scrape cannot tell this from GLYPHLESS_COMPACT — transcript path is the real defense");
  assert.equal(r.options.length, 3);
});

// --- arrow-driver core ----------------------------------------------------

test("planArrowSteps clamps to top then steps down to the target", () => {
  assert.deepEqual(planArrowSteps(5, 0), { ups: 5, downs: 0 });
  assert.deepEqual(planArrowSteps(5, 2), { ups: 5, downs: 2 });
  assert.deepEqual(planArrowSteps(5, 4), { ups: 5, downs: 4 });
});

test("planArrowSteps rejects out-of-range / invalid input", () => {
  assert.equal(planArrowSteps(5, 5), null, "index == count is out of range");
  assert.equal(planArrowSteps(5, -1), null);
  assert.equal(planArrowSteps(0, 0), null, "no options");
  assert.equal(planArrowSteps(3, 1.5), null, "non-integer index");
  assert.equal(planArrowSteps(undefined, 0), null);
});

test("highlightedOptionIndex returns the sole selected row", () => {
  const opts = [{ selected: false }, { selected: true }, { selected: false }];
  assert.equal(highlightedOptionIndex(opts), 1);
});

test("highlightedOptionIndex is -1 when none or several are selected", () => {
  assert.equal(highlightedOptionIndex([{ selected: false }, { selected: false }]), -1);
  assert.equal(
    highlightedOptionIndex([{ selected: true }, { selected: true }]),
    -1,
    "ambiguous multi-highlight must not commit"
  );
});

test("classic numbered prompts keep parsing without a footer", () => {
  // Regression: the footer requirement must apply only to the description path.
  assert.ok(detectPrompt(TRUST_FOLDER), "trust-folder has no footer, no descriptions");
  assert.ok(detectPrompt(PLAN_MODE_APPROVAL), "plan-mode has no footer");
});
