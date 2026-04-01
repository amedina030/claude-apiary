# Budgeter — tune.py Roadmap

## Current state (v1)
- Adjusts rule weights based on per-rule precision from feedback.jsonl
- Requires minimum samples per rule before suggesting changes (default: 5)
- Manual confirm before writing to config.json
- Not enough data yet to produce suggestions (15 tasks, most rules < 5 samples)

## v2 — Keyword suggestions from log analysis

Analyze actual assistant messages in usage_log.jsonl to suggest additions/removals to keyword lists.

### Approach
1. Join feedback.jsonl against usage_log.jsonl to label each task as expensive or cheap (using the existing percentile threshold)
2. For each task, collect the assistant messages (planning message at minimum, optionally all messages in the task)
3. Compute word frequencies (or TF-IDF scores) separately for expensive tasks and cheap tasks
4. Words significantly over-represented in expensive tasks that aren't in any current keyword list → candidates to **add** to scope_keywords or breadth_keywords
5. Words in current keyword lists that appear equally in both expensive and cheap tasks → candidates to **remove** (they aren't discriminating)
6. Present suggestions as a ranked list with frequency/score data; human confirms each change

### Output format
```
KEYWORD SUGGESTIONS

  Candidates to ADD to scope_keywords:
    "overhaul"    — 4x more frequent in expensive tasks (8/12 exp, 2/40 cheap)
    "consolidate" — 3x more frequent in expensive tasks (5/12 exp, 3/40 cheap)

  Candidates to REMOVE from scope_keywords:
    "migrate"     — appears equally (2/12 exp, 5/40 cheap, ratio ~1.0)

  Apply suggestion 1? [y/N]
```

### Prerequisites
- 30+ tasks with user_message populated (for investigative_keywords context)
- 50+ tasks total for meaningful TF-IDF signal
- v1 weight tuning should have run at least once

## v3 — New rule group proposals

Discover clusters of words that predict expensive tasks but don't belong to any existing keyword group.

### Approach
1. Same expensive/cheap split as v2
2. After filtering out words already in existing keyword lists, find words over-represented in expensive tasks
3. Cluster these by co-occurrence (words that tend to appear together in the same tasks)
4. Each cluster is a candidate new rule group — present with a suggested name and member words
5. Human names the group, confirms the word list, and sets an initial weight

### Example output
```
PROPOSED NEW RULE GROUP

  Cluster: ["setup", "install", "configure", "environment", "deploy"]
  Appears in: 6/12 expensive tasks, 1/40 cheap tasks
  Suggested name: deployment_keywords
  Suggested weight: 1.0

  Add this group? [y/N]
```

### Prerequisites
- 100+ tasks for reliable clustering
- v2 keyword tuning should have run to ensure existing lists are up to date

## v4 (future) — Lightweight classifier

Replace or supplement rules with a trained model.

### Approach
- Train logistic regression or small sentence transformer on feedback.jsonl (binary: expensive/cheap)
- Input: user message + first assistant message, concatenated
- Inference at warning time: score the current task, warn if P(expensive) > threshold
- Falls back to rule-based scoring if model file is missing or stale

### Why not now
- Needs 100+ labeled examples minimum for logistic regression, 500+ for a transformer
- Adds inference latency and a model file to manage
- Retraining needed when patterns shift (new project types, different usage patterns)
- feedback.jsonl is the labeled dataset — it's accumulating automatically

### Migration path
- Rules stay as a fast fallback
- Model score and rule score could be combined (weighted average)
- tune.py would gain a `--train` mode that fits the model and reports cross-validation accuracy
- Only switch to model-primary when CV accuracy consistently beats rule precision

## Key constraint across all versions
tune.py is always **propose + human confirm**. No version auto-writes config changes or model files.
