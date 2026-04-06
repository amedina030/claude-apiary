| Slug | Title | Status | UUID | Notes |
|------|-------|--------|------|-------|
| gitignore-pipeline-runtime-artifact-directories | Gitignore pipeline runtime artifact directories | done | dc1ed814-3316-488b-83ce-63966ffdfe3b | |
| pretooluse-hook-enforcing-cli-lookup-before-repo-cli-use | PreToolUse hook enforcing cli_lookup before repo CLI use | done | c7958c29-6650-40be-99cb-9935f9c35de8 | step 3 completed manually after executor's git-commit step failed silently — see follow-up tickets for executor stdout/stderr handling and run.py truncation |
| executor-surfaces-git-stdout-on-commit-failures | Executor surfaces git stdout on commit failures | done | | hand-fixed manually, not via pipeline |
| pipeline-run-py-shows-full-stage-stderr-on-failure | Pipeline run.py shows full stage stderr on failure | done | | hand-fixed manually, not via pipeline |
| make-claude-apiary-fully-portable-across-machines-and-oses | Make claude-apiary fully portable across machines and OSes | backlog | | |
| document-portability-rules-in-memory-notes-and-claude-md | Document portability rules in memory notes and CLAUDE.md | done |  | memory files + MEMORY.md index + CLAUDE.md Portability section + scribe notes #174/#175 + settings.json audit appended to portability epic context. Scope item 8 (/dev/null line) skipped — that string lives in the harness env block, not user CLAUDE.md. |
| add-request-id-grouping-to-budgeter-for-multi-task-chains | Add request_id grouping to budgeter for multi-task chains | done |  | log_agent_cost.py --request-id flag, pipeline/run.py threads pipeline_uuid as request_id, report.py --by-request view. Smoke-tested end-to-end. Backward compat: pre-existing entries bucket into (no request). |
| overnight-cron-pipeline-with-morning-branch-review-workflow | Overnight cron pipeline with morning branch review workflow | failed | 5029f36e-35af-4923-9dd8-d9139bfde8ff | |
| pipeline-mark-done-cli-for-hand-fixed-tickets | Pipeline mark-done CLI for hand-fixed tickets | done |  | self-hosted: closed itself after build + smoke test |
| refine-to-plan-file-handoff-to-cut-auto-plan-token-cost | Refine-to-plan file handoff to cut auto_plan token cost | backlog | | |
