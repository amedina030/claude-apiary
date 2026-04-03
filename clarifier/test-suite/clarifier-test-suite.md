# Clarifier Test Suite

## Overview

This file contains 24 test cases for validating clarifier v2 (assumption detector) behavior. Each test specifies content to analyze, whether the clarifier should detect assumptions, and what assumptions it should flag if it does.

Test fixture files are located at `~/.claude/test-fixtures/`:
- `test-doc-a.md` — design notes doc with duplicates and resolved items
- `test-doc-b.md` — release checklist
- `config-sample.md` — configuration reference with incomplete sections

## What These Tests Cover

The 24 test cases validate assumption detection — whether the clarifier flags assumptions when it should, returns clean when it shouldn't, and identifies the right assumptions.

## Checking Results

**Manual:** Send content to the clarifier agent and compare the JSON output against expected assumptions below.
**Automated:** Feed each test case via `/run-clarifier-tests`; the runner checks that (a) assumptions were detected when expected, and (b) the flagged assumptions cover the expected topics.

---

## Category 1: Should Fire — Scope Ambiguity

### Test 1 — Scope: Clean up file

**Prompt:** `Clean up the config file.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which config file? (no file is specified)
- What does "clean up" mean — formatting only, or content changes too?
- Should incomplete sections (e.g., Cache) be filled in or left alone?

---

### Test 2 — Scope: Update documentation

**Prompt:** `Update the documentation.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which documentation file?
- What kind of updates — new content, corrections, formatting?
- Is there a specific section to target?

---

### Test 3 — Scope: Refactor

**Prompt:** `Refactor the code in test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- test-doc-a.md is a markdown doc, not code — clarify what "refactor" means here
- Which section or content should be restructured?
- What is the desired end state?

---

### Test 4 — Scope: Improve a document

**Prompt:** `Improve test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- What does "improve" mean — structure, content, clarity, completeness?
- Which sections should be targeted?
- Are there specific problems to fix, or is this open-ended?

---

## Category 2: Should Fire — Pronoun / Referent Ambiguity

### Test 5 — Referent: Move it

**Prompt:** `Move it to the other section.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- What is "it"? (no prior context establishes a referent)
- Which file is this in?
- Which section is "the other section"?

---

### Test 6 — Referent: Delete the duplicate

**Prompt:** `Delete the duplicate in test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which duplicate — Section 1 has a duplicate Decision A, Section 2 has a duplicate Q3
- Should both copies be removed, or just one — and if one, which?

---

### Test 7 — Referent: Swap them

**Prompt:** `Swap them.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Swap what? No referent established
- In which file?
- What does "swap" mean here — reorder, exchange content, rename?

---

### Test 8 — Referent: Put it back

**Prompt:** `Put it back.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- What is "it"?
- Back where? No prior state or location established
- Which file is involved?

---

## Category 3: Should Fire — Conflicting Constraints

### Test 9 — Conflict: Shorter but more comprehensive

**Prompt:** `Make test-doc-a.md shorter but more comprehensive.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- These goals conflict — which takes priority if they can't both be achieved?
- What should be cut to make it shorter?
- What should be added to make it more comprehensive?

---

### Test 10 — Conflict: Simplify but keep everything

**Prompt:** `Simplify config-sample.md but don't remove any options.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- If nothing can be removed, what does "simplify" mean here — formatting, wording, structure?
- Are new options allowed to be added, or is the field set frozen?

---

### Test 11 — Conflict: Faster without structural change

**Prompt:** `Make it faster without changing the structure.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- What is "it"? No referent established
- What does "faster" mean in the context of a markdown document?
- What counts as a structural change?

---

### Test 12 — Conflict: Rewrite but keep the same

**Prompt:** `Rewrite test-doc-b.md to be cleaner but keep everything exactly the same.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- "Rewrite" and "keep everything exactly the same" are contradictory — clarify which takes priority
- What specifically should change to make it "cleaner"?

---

## Category 4: Should Fire — Implicit Assumptions

### Test 13 — Implicit: Delete resolved items

**Prompt:** `Delete the resolved items in test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which items count as "resolved"? (some are marked RESOLVED, but the criteria aren't defined)
- Should resolved items be deleted entirely, archived, or moved to a separate section?
- Decision A appears twice as RESOLVED — delete both occurrences?

---

### Test 14 — Implicit: Move important sections to top

**Prompt:** `Move the important sections to the top of test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which sections are "important"? This is a judgment call
- Top of the file, or top of a specific section?
- Should unimportant sections be removed or just reordered?

---

### Test 15 — Implicit: Add missing fields

**Prompt:** `Add the missing fields to config-sample.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which fields are "missing"? Missing relative to what standard or schema?
- The Cache section says "fields to be determined" — does that count as missing?
- Should new sections be added, or only fields within existing sections?

---

### Test 16 — Implicit: Fix inconsistencies

**Prompt:** `Fix the inconsistencies in test-doc-a.md.`

**Should detect assumptions:** Yes

**Expected assumptions:**
- Which inconsistencies? (duplicates? formatting? conflicting decisions?)
- What is the source of truth to fix against?
- Should duplicate entries be merged or removed?

---

## Category 5: Should NOT Fire — Clearly Scoped

### Test 17 — Clear: Read a file

**Prompt:** `Read test-doc-a.md.`

**Should detect assumptions:** No

**Why it's clear:** Single named file, single action (read), zero assumptions needed, fully reversible.

---

### Test 18 — Clear: Append a specific line

**Prompt:** `Add a line saying "Last updated: 2026-03-13" to the bottom of test-doc-b.md.`

**Should detect assumptions:** No

**Why it's clear:** Exact content specified, exact location specified, single named file, easily undone.

---

### Test 19 — Clear: Count sections

**Prompt:** `How many sections are in test-doc-a.md?`

**Should detect assumptions:** No

**Why it's clear:** Read-only question, single named file, one unambiguous answer.

---

### Test 20 — Clear: Show file contents

**Prompt:** `What is in config-sample.md?`

**Should detect assumptions:** No

**Why it's clear:** Read-only, single named file, no judgment required.

---

## Category 6: Should NOT Fire — Trivially Obvious

### Test 21 — Trivial: List files

**Prompt:** `What files are in this directory?`

**Should detect assumptions:** No

**Why it's clear:** Unambiguous read-only system query, no assumptions, no file modifications.

---

### Test 22 — Trivial: Show first lines

**Prompt:** `Show me the first 5 lines of test-doc-a.md.`

**Should detect assumptions:** No

**Why it's clear:** Exact line count specified, single named file, read-only.

---

### Test 23 — Trivial: Status check

**Prompt:** `Is the clarifier on?`

**Should detect assumptions:** No

**Why it's clear:** Yes/no system state query, zero assumptions, no file modifications.

---

### Test 24 — Trivial: Off-topic general question

**Prompt:** `How do I make a cake?`

**Should detect assumptions:** No

**Why it's clear:** General knowledge question, no files involved, no assumptions needed, no actions taken.

---

## Pass / Fail Criteria

A test **passes** if:
- Assumptions were detected when expected (or `clean: true` returned when expected)
- If assumptions were detected, they cover all the topics listed under **Expected assumptions** (wording need not match exactly — topic coverage is what matters)
- Severity ratings are reasonable for the context

A test **fails** if:
- Assumptions were detected when they shouldn't have been (over-trigger)
- No assumptions were detected when they should have been (under-trigger)
- A key expected assumption topic was missed
