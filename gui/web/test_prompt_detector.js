// Unit tests for prompt_detector.js. Run with `node test_prompt_detector.js`
// (no npm dependencies). The detector operates on the rendered-and-trimmed
// xterm buffer as a string[]; these fixtures model the shapes we've observed.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { detectPrompt } = require("./prompt_detector");

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

test("classic numbered prompts keep parsing without a footer", () => {
  // Regression: the footer requirement must apply only to the description path.
  assert.ok(detectPrompt(TRUST_FOLDER), "trust-folder has no footer, no descriptions");
  assert.ok(detectPrompt(PLAN_MODE_APPROVAL), "plan-mode has no footer");
});
