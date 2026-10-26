# Cline hybrid checkpoints — measured configuration

Installed Cline CLI 3.0.74. Connections already authenticated; no addons installed. Use explicit native CLI provider/model overrides for each checkpoint. These instructions do not install an automatic model-switching plugin, and `providers.json` mode entries are not used to invent unsupported Plan/Act mappings.

## Run sequence

Use the same disposable repository path for every phase. Each command starts a fresh session: do not supply `--id`. Supply the appropriate task packet as the final prompt argument. Save JSON stdout and stderr for every invocation, including failures.

1. Local read-only discovery:
   `cline --json --plan --provider openai-compatible --model qwen-3.8-nf-splash --cwd REPO --retries 2 --timeout 180 PROMPT`
2. Cloud planning from the task, complete discovery report and relevant source:
   `cline --json --plan --provider openai-codex --model gpt-6.1-sol --thinking medium --cwd REPO --retries 2 --timeout 180 PROMPT`
3. One local implementation worker from the task and approved plan:
   `cline --json --provider openai-compatible --model qwen-3.8-nf-splash --cwd REPO --retries 2 --timeout 600 PROMPT`
4. Host runs unchanged ordinary and independent checks, verifies scope and constructs complete patch/source/test evidence. Fresh cloud semantic review uses the command in step 2 and requires `VERDICT: PASS`.

The prompt contract permits at most two implementation attempts, no delegation, and only the task's allowed files. CLI `--retries 2` is its consecutive-mistake guard; it does not itself enforce the implementation-attempt budget. Host gates remain necessary. CLI native default tool approval was used in the disposable fixture; no `--yolo` flag or permission changes were introduced.

Your existing local provider has reasoning disabled. This run preserved that setting; the earlier OpenCode local run had thinking enabled. Cloud effort was medium in both. Changing local thinking would require a separately recorded run for a fully controlled comparison. Do not silently overwrite these measured results.

## Result, 26 October 2026

| Harness/workflow | Cloud tokens/completed task | Local tokens | Active runtime |
|---|---:|---:|---:|
| OpenCode direct cloud (saved baseline) | 50,232 | 1,346 maintenance | 75.3 s |
| OpenCode hybrid (saved baseline) | 13,913 | 140,731 | 102.3 s |
| Cline hybrid | 23,595 | 272,424 | 143.4 s |

Cline cloud plan: 12,546 tokens. Cloud review: 11,049. Local discovery: 195,544. Local implementation: 76,880. Two cloud requests and 22 local requests, including one failed discovery tool call and its recovery. Implementation passed in one attempt; there was no corrective implementation or transport retry. Twelve unit tests, eight explicit independent cases, 200 seeded probes and mutation independence passed. Cloud and parent semantic review accepted the complete patch. Scope and frozen checker hash were preserved.

Cline used 53.0% fewer cloud tokens than saved OpenCode direct cloud, but 69.6% more than OpenCode hybrid. It used 47.0% of the direct-cloud baseline and missed the target ceiling of 30%. Both hybrid workflows made two cloud requests; the difference lies in request size, not additional cloud turns. Cline's longer discovery report increased cloud-plan input. Review also carried a different native system/tool prompt and a longer implementation/docstring; these are plausible contributors, not a precisely isolated causal split.

Cline wall time from first launch to final review completion: 225.6 s including manual checkpoint preparation and host checks. Active invocation time excludes gaps. OpenCode's recorded checkpoint wall time was 343 s. Manual preparation times are not stable harness performance measurements.

## Accounting and evidence

Native Cline inputTokens includes cached input. outputTokens includes reasoning. Sum inputTokens + outputTokens; do not add cacheReadTokens or reasoningTokenCount again. Per-request usage sums reconcile to each run_result.aggregateUsage and exported native session message metrics. No subagents or additional sessions were launched for the fixture. All four sessions and raw output are retained. No unknown-usage failed API request was observed.

The comparison covers native OpenCode/Cline execution, including planning, review, tool failures and retries. It excludes Codex chat setup/reporting, matching the prior requested scope; this is not an end-to-end cloud cost for this chat. Counts are tokens, not money. This is one small task and a saved-baseline comparison; local thinking settings differ, so it is not a perfectly controlled same-settings experiment. No new direct-cloud Cline arm was run.

Evidence: `/Users/oleksii.naboichenko/benchmark-evidence/cline-hybrid-20261026T181256Z`. See `results.json`, `sessions.json`, `native-sessions/`, per-phase `events.jsonl`, `usage.json`, `argv.json`, and `execution.json`, `complete.patch`, `acceptance.json`, `host-checks.json`, and verified recovery archives. Frozen seed/checks remain reusable.

The main production code was not changed. These instructions are also saved at `~/.cline/HYBRID-USAGE.md` for reuse. The native model picker remains explicit: choose cloud for Plan/review and Qwen for Act, or use the pinned CLI commands above. Automatic model switching is not configured or claimed.
