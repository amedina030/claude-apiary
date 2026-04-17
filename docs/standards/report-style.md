---
type: standard
title: Report Style
scope: project
description: How to write acceptance, validation, and post-mortem reports — required sections, length cap, and where each kind lives
framework_version: "1.0"
last_verified: 2026-04-17
---

# Report Style

How to write reports that document something that happened. Applies to three related report types that share the same skeleton:

| Kind | When | What it captures |
|------|------|------------------|
| **Acceptance test** | A feature or epic finished its fixture suite; now verifying it against real inputs | Whether the feature works on real data, not whether tests pass |
| **Live-run validation** | Running a tool against a real system for the first time (e.g. `cron_health repair --apply` on the actual Task Scheduler) | Whether the tool behaves against live state; what broke on first contact |
| **Post-mortem** | An incident happened; writing up what went wrong | Timeline, root cause, what was fixed, what's still at risk |

If the work you did doesn't fit one of these three, you probably don't need a report — a commit message, handoff note, or decision log is enough. Don't write reports for ordinary feature work.

## Where reports live

Default: a scribe context note (`notes.py add --type context --content "<report>"`). Reports decay; scribe is the right durability for that.

Escalate to `docs/reports/<kind>/<yyyy-mm-dd-slug>.md` only when:
- The report will be referenced in future work (e.g. a portability baseline or a schema-migration log)
- Multiple people need to find it without knowing the session it came from
- It's load-bearing evidence for a decision record in `docs/`

If you're unsure, start in scribe. Moving later is cheap; migrating a stale `docs/reports/` entry back to scribe is awkward.

## Length

**Aim for 200–500 words.** If a report runs substantially longer, it's almost always either:
- Two reports in one (split by phase, or by kind)
- Prose that belongs in the commit message or code comments
- Narration of what the tools said — trim to outcomes

A report that fits on one screen is more likely to actually get read. Length cap is a signal, not a rule — exceed it when the work genuinely needs it, but check first.

## Required sections

Every report contains these, in order. Omit a section only when it would be empty after genuine effort to fill it.

### Heading line

`# <Kind>: <what this report is about> (<yyyy-mm-dd>)`

Example: `# Acceptance: runner --target-repo on non-apiary repo (2026-04-20)`

### Goal

One sentence. What were you verifying, validating, or diagnosing? If you can't state the goal in one sentence, the scope is wrong.

### Environment

The specific surface the work happened on. Include whatever a future reader would need to reproduce or interpret:

- OS + version (`Windows 11 26200`, `macOS 14.5`, etc.)
- Commit SHA or branch at the time
- Relevant config values or flags
- External systems touched (real databases, real APIs, real schedulers)

"On my machine" is not an environment.

### Procedure

Ordered steps of what was actually done. Past tense, specific, minimal. Skip narration of your thought process — just what the reader would need to replay the same steps.

### Results

What actually happened. Not what you hoped would happen. For each step or assertion:
- **Pass** — what you observed that confirmed the expected behavior
- **Fail** — what you observed that contradicted it, plus the diagnostic signal (error message, exit code, file contents)

Keep results tied to specific procedure steps — don't mix "results" and "reflection."

### Gaps & follow-ups

What this report does NOT cover, and what should happen next. Each item is either:
- A **follow-up ticket** filed (with its scribe ID)
- A **known limitation** that's explicit and accepted
- A **next step** for the same work stream

An empty "Gaps & follow-ups" section means you claim the work is complete. That's rare — default to listing at least one gap.

### Author + date

`Author: <name>  Date: <yyyy-mm-dd>`

The date is when the report was *written*, not when the work happened. If they differ, note both.

## Tone

Per [doc-style.md](doc-style.md): direct, present tense, active voice, specific file paths and flag names. One extra rule for reports: **use past tense when describing the procedure and results** — they describe something that already happened. The rest of the report (environment, gaps) follows the standard doc-style rules.

## Template

Copy and fill in:

```markdown
# <Kind>: <topic> (<yyyy-mm-dd>)

## Goal

<One sentence.>

## Environment

- OS:
- Commit:
- Config:
- External systems:

## Procedure

1.
2.
3.

## Results

- Step 1 → <pass/fail + observed signal>
- Step 2 → <pass/fail + observed signal>
- Step 3 → <pass/fail + observed signal>

## Gaps & follow-ups

-

---
Author: <name>  Date: <yyyy-mm-dd>
```
