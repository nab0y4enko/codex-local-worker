# Audit: the 3.17M setup/reporting snapshot

The saved snapshot covers the comparison request beginning2026-10-26T03:58:40.616Z through
2026-10-26T04:18:28.549Z. In America/New_York, that is October25 23:58 through October26 00:18.
It is a lower-bound snapshot, not the final total after the remaining report and response.

| Phase | Cloud responses | Input plus output |
|---|---:|---:|
| Setup before first measured model | 5 | 1,258,750 |
| Execution coordination | 4 | 1,075,904 |
| Accounting/reporting | 3 | 831,162 |
| Total | 12 | 3,165,816 |

Of the total,3,096,320 tokens were cached input (97.8%). New/uncached input was39,563; output
was29,933. Reasoning was21,685 of those output tokens, not an additional bucket; the remaining
visible output was8,248. The median input per response was264,186, ranging241,581–277,897.
Twelve code-orchestration tool calls had28,897 bytes of arguments, including bespoke setup/run/
account/report code. These are measurements of the outer chat, not local-model implementation.

## Root cause

The prior fix reduced model turns inside the measured task arms, but left the outer experiment
model-driven. The cloud kept rebuilding the harness, inspecting configuration/scripts, checking
progress and generating accounting/report code in a very long conversation. Every subsequent
response replayed its large inherited context. The measured six task arms cost525,207 cloud
tokens separately; those counters were not included twice in this parent snapshot.

The mistake was optimizing the child workflow while continuing ad hoc model orchestration around
it. Cached context is still counted in raw cloud-token totals. These numbers do not imply3.17M
new generated words, dollar cost, or a known subscription-limit charge. Aggregate counters locate
turn/phase overhead but cannot prove the exact causal token cost of each instruction or file.

## Fix

Use the reusable deterministic qualification command instead of cloud-authoring a new harness:

```sh
bin/local-worker-qualification run --output-dir /absolute/fresh/evidence
bin/local-worker-qualification report --evidence-dir /absolute/saved/evidence
bin/local-worker-qualification audit --snapshot /absolute/saved/parent-overhead-snapshot.json
```

The runner preserves the frozen kit: Sol low plans/reviews and implements the cloud control,
Luna low implements its control, and the launcher discovers/pins the local model. It does not
change the model/provider selected in the desktop window.

The first command is a dry plan unless explicit `--live` is supplied. When invoked from an
installed cache, a future live run also needs `--source /absolute/addon-git-checkout`; the cache
has no Git source metadata. This requirement is checked before staging or inference. Report/audit are offline
code paths: no model request, discovery, credentials or extra reviewer. Live opt-in executes the
retained frozen stages sequentially, then final accounting/report generation, in one controller
process. Existing campaigns are never overwritten or silently restarted. All original tests,
independent checks, semantic decisions, retry/deadline budgets, failed runs, usage categories and
verified recovery archives remain. Missing usage stays unavailable; it is never changed to zero.

The cloud should issue one controller command, then read the final concise report once. Waiting,
state inspection, accounting and Markdown generation belong to code, not repeated model turns.
Do not launch noop preparation/reviewer sessions or use the model as a polling timer. Existing
saved runs can be reported again without any new inference.

This cannot delete this chat's existing history or shrink Codex's fixed system/tool context. A
small number of responses in this long chat can still replay hundreds of thousands of tokens.
The code removes avoidable harness/reporter turns; no new measured live-savings claim is made.
No benchmark was run for this fix. Regression coverage also rejects symlink-parent output
inside the source checkout, propagates failed stage exits, and prevents reuse of fixed narrative
numbers from an earlier campaign. Offline checks establish safe command behavior, not a token
percentage.

Evidence: `~/benchmark-evidence/per-task-inline-20261026/parent-overhead-snapshot.json`,
`parent-counter-records.json`, and
`~/.local/share/codex-local-worker/setup-report-overhead-20261026/saved-audit.json`.

## Verification

The initial local draft was semantically rejected despite passing its nine tests; its archive
and failure record remain. A bounded repair added regressions for stale narratives, nonzero
stage exits, unknown totals, arithmetic identities, symlink output and Git-source preflight.
The repaired16-test suite passed under the normal permission wrapper. The same cloud planner
made an explicit digest-bound acceptance; original tests plus actual offline report/audit
commands passed during native integration, and recovery archival was verified.

The unchanged full host suite passed1,187 tests with the same eight live tests skipped. Actual
replay of the old saved report and audit used zero model calls and reproduced the failed-task
cost and3,165,816 total. No new live benchmark or OpenCode model request was made. Code/tests
were implemented by the local worker; the parent authored this audit, recovery guide and
command recipes.
