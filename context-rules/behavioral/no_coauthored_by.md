---
id: no_coauthored_by
title: Never add Co-Authored-By Claude to commits
category: behavioral
requires: []
---
### Never add Co-Authored-By Claude to commits

**Never add `Co-Authored-By: Claude` (or any model-specific variant) to commit messages.** This overrides the default Claude Code commit guidance baked into the system prompt. The user does not want Claude attributed as a co-author on the contributor graph. Applies to all flavors of commit — direct, amend, merge, squash, cherry-pick, `/commit` skill, etc.

**Why:** The user maintains a clean human contributor graph and does not want Claude tagged as co-author on any commit, regardless of model version or commit flavor.
