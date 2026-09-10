---
type: standard
title: Prose Style
scope: project
description: The machine-writing tells the prose linter checks in notes and documents, the scope rule behind it, and how to add or calibrate a rule
framework_version: "1.0"
last_verified: "2026-09-10"
---

# Prose Style

Rules for the prose in scribe notes, learnings, memory files and documents. They are enforced by the prose linter (`prose/cli.py`, rules under `prose/styles/Tells/`), which runs on every markdown Write/Edit as an advisory and on every `scribe/notes.py add` and `learn`. The per-repo flag `prose-gate` (`core/flags.py enable prose-gate`) turns error-level findings into a block.

## Why cadence and not vocabulary

Every tell below is a rhythm. That is what makes it survive editing: you can swap every noun in a sentence and keep the shape that gives it away. It is also why rereading a draft rarely catches one, since the eye slides over familiar rhythm. Counting works where reading does not, and that is the whole justification for the tool. It also bounds the tool: anything that needs a judgement about meaning stays out of scope (see "What the check cannot catch").

The underlying failure is writing that performs a quality instead of having it. It performs candour, or balance, or insight, where a person would be candid, be balanced, or say the thing.

## The scope rule

The linter parses the Markdown before it counts anything. Paragraphs, list items, headings, blockquotes and table cells are separate scopes, and code spans, fenced blocks, front matter and HTML are never scored. Each rule names the scopes it applies to.

This matters because most of apiary's prose is bullet-shaped, and because code must never be scored: a semicolon in a shell command or a dash in a code span is not prose. The first measurement without any scoping flagged 70% of notes, much of it on code and table syntax. With scoping, each rule names where it applies. The two punctuation rules apply everywhere outside code by decision (D-2026-63): an em-dash in prose is a tell wherever it sits, including a label in a bullet (`Owner — platform` reads as well with a colon), and a semicolon is a tell everywhere but a table cell.

## Rules

Levels: `error` blocks when the gate is on, `warning` and `suggestion` are printed and never block. `prose/cli.py rules --verbose` prints every rule with the reason its threshold has the value it has.

| Rule | Level | Scope | What it catches |
|------|-------|-------|-----------------|
| `CandourPhrases` | error | text | Fixed idioms that announce honesty: `worth noting`, `to be fair`, `full disclosure`, `in all honesty`, `truth be told` |
| `CandourShape` | error | sentence | The productive form: `I want to be clear/exact/honest`, `to be honest,`, `let me be direct` |
| `EmDash` | error | text | Any em-dash outside code: paragraphs, bullets, headings, blockquotes, table cells |
| `Semicolon` | error | paragraph, list, blockquote, heading | Any semicolon outside code and table cells |
| `NegationCorrection` | warning | text | `not X, it is Y`, `not just X but Y`, `X rather than being Y`, `It wasn't X. It was Y.` |
| `SectionLabel` | warning | text | A fragment announcing the next sentences: `On the schema question.` |
| `SummaryCloser` | warning | text | `In summary`, `In conclusion`, `Overall,` opening a sentence |
| `ProcessNarration` | warning | paragraph | A paragraph opening with `Let me`, `I'll now`, `Certainly,`, `Here's a` |
| `AiVocabulary` | warning | text | `delve`, `tapestry`, `testament to`, `pivotal`, `seamless`, `meticulous`, `holistic`, `deep dive`, `game-changer`, `it's important to note` |
| `Repetition` | warning | paragraph, list, heading, blockquote | A word doubled (`the the`) |
| `EpigramContrast` | suggestion | sentence | `X is a Y, not a Z` |
| `AbstractTriad` | suggestion | text | Three abstract nouns joined by commas and `and` |
| `JargonSwaps` | suggestion | text | `utilize`, `leverage`, `facilitate`, `in order to`, `prior to`, `a number of` |

Quoting a tell in a document is fine: put it in backticks, which the parser never scores, or wrap a region in `<!-- prose off -->` and `<!-- prose on -->`.

## Examples

Each pair is a real shape, rewritten. The rewrite is shorter every time, which is usual: most of these patterns add words.

**Em-dash as the default connector.** Fine once on a page. A problem when it becomes the general-purpose joint between clauses.

> before: `the index rebuild needs a lock — nobody wants a surprise at 2am`
> after: `the index rebuild needs a lock`

**Negation-correction.** Stating what a thing is not, then correcting to what it is. It stages a small reversal the reader never asked for.

> before: `The export job is not a batch process, it is a streaming consumer.`
> after: `The export job is a streaming consumer.`

> before: `the team would be adopting the scheduler rather than being trained on it`
> after: `nobody on the team has used the scheduler`

Not every "rather than" is a tell. `TOML rather than YAML` is a factual contrast. The tell is when the negated half exists only to set up the positive half.

**Announcing candour.** A sentence that announces its own honesty before it says anything.

> before: `On scope I want to be exact, because the plan assumes a shared cache: that cache belongs to the storage group, not to this service.`
> after: `The plan assumes a shared cache. The storage group owns it.`

That one sentence carried three tells: the candour announcement, the colon pause and the negation ending.

**Section-label sentences.** A fragment that announces what the next few sentences cover.

> before: `On the schema question. The ledger table keeps its name...`
> after: `The ledger table keeps its name...`

**Summary closers and process openers.** A note records outcomes. It does not narrate its own writing or restate its own paragraphs.

> before: `Let me walk through where things stand. ... In summary, the cutover is on track.`
> after: `The cutover is on track. ...`

**Semicolon as a dramatic pause.** Two clauses joined for effect rather than because they are coordinate.

> before: `We are not guessing at the cause; we have run the consumer for two quarters.`
> after: `We have run the consumer for two quarters and know the cause.`

## Running the check

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py check <file>...
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py check --stdin < draft.md
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py rules --verbose
```

`check` exits 1 when any finding is at `error` level, so the same command serves a CI step. Each finding carries the line, the matched span and the surrounding text, because "3 negation-corrections" sends the writer back to reread the whole draft, which is the failure the tool exists to fix.

The `/prose <path>` skill runs the check and rewrites until clean, then does the three reads below.

## What the check cannot catch

Three tells have no countable shape. A clean report is necessary and not sufficient, and the report says so every time it runs.

- **Aphoristic closers.** A sententious one-liner that summarises a paragraph's moral. Test: if the sentence would work as a pull quote, delete it.
- **Mirror constructions.** Repeating a word in a balanced frame to sound considered ("the half of the plan about standards is the half we know best"). Say it once.
- **The rule of three.** Triads of adjectives or clauses arriving by reflex rather than because there are exactly three things. `AbstractTriad` catches the noun form; the rest needs a read.

## Adding a rule

One TOML file per rule under `prose/styles/Tells/`. The filename is the rule id.

```toml
extends = "existence"       # existence | substitution | occurrence | density | repetition
message = "Announced candour '%s': cut the announcement and state the thing."
summary = "No announcing candour. Say the thing."   # one line, shown in the hook reminder
level = "error"             # error | warning | suggestion
scope = ["text"]            # paragraph | list | heading | blockquote | table | text | sentence | raw
ignorecase = true
tokens = ["worth noting"]   # literal, word-bounded
raw = ['\bI want to be\s+(?:clear|exact)\b']   # Python regex; \A anchors to the segment start
why = "Where the rule and its threshold came from. Required."
credit = "adapted from <source> (licence)"     # when mined from elsewhere
```

Check types: `existence` reports every match; `substitution` adds a `[swap]` table of pattern to preferred word; `occurrence` counts `token` per block against `min`/`max`; `density` counts `token` over all in-scope blocks against `max(floor, words * rate / per_words)`; `repetition` finds doubled words with an `exceptions` list.

Every rule must fire at least once on `prose/styles/Tells/fixtures/ai-prose.md` and never on `fixtures/clean-prose.md`. `prose/test_rules.py` enforces both, so a new rule needs a line in the first fixture.

A repo can disable rules or narrow the checked paths in `<repo>/.claude/prose.json` (same keys as `prose/config.json`, shallow merge).

## Calibrating

Do not ship a threshold without measuring it:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py calibrate <dir>... --min-level warning
```

Run it over prose a reader has already accepted. Every hit on that corpus from an `error`-level rule is a false positive: tighten the pattern or demote the rule, and record what the fix was in its `why`. The exception is a corpus that is itself machine-written, which is what apiary's own notes were at the first release. The two punctuation rules were promoted to `error` on that basis, by decision, with the hits recorded in their `why`. Every miss on a draft a reader called machine-written is a gap: widen the pattern or add a rule. The step that gets skipped, and the one that validates the whole thing, is rewriting a flagged draft until the tool reports clean and then having a person read it. If it still reads machine-written, the gap is in this document rather than in the tool.

Measured at the first release (2026-09-10) over 918 notes, learnings, memory files and docs: the unscoped checker flagged 61% of files, almost all on structural punctuation in bullets. With scoping, 44% carried a warning-level finding and none an error-level one. Later that day the punctuation rules became `error` with no budget at all. `EmDash` allows none outside code, and `Semicolon` allows none outside code and table cells. The same corpus now carries error-level findings in most of its files. That is the intended bite: the gate is on in main-apiary, and a note with a semicolon or an em-dash does not save until it is rewritten.
