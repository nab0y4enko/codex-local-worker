# Remaining orchestration overhead: saved-run audit

This audit uses existing evidence only. No live benchmark was run for this repair.

## What the updated pilot actually spent

The one-task pilot under `~/benchmark-evidence/updated-small-20261026` recorded:

| Arm | Cloud responses in planner/reviewer | Total cloud tokens | Seconds |
| --- | ---: | ---: | ---: |
| Direct cloud | 6 | 112,222 | 50.3 |
| Local worker | 10 | 259,244 | 167.4 |
| Direct Luna | 9, plus Luna implementation | 236,313 | 54.7 |

All three passed correctness and semantic review without implementation repair. Local cloud
planning/dispatch cost 54,601 tokens; review/integration/closeout cost 204,643 (78.9%). The local
planner read `review.md` (19,728 bytes), then separate trusted inputs/logs (3,697), the canonical
package (20,942), and full receipt/summary (7,892). These follow-up reads added context that later
responses replayed. The final three responses alone cost 115,555 tokens. File-size and response
counts identify duplication; they do not prove an exact causal token charge for each file.

The first local-arm cloud response already carried 15,099 input tokens (12,288 cached). Later
responses reached 40,371 input tokens each. The desktop system/tool context remains even when
implementation runs locally. Reducing model handoffs matters as well as reducing evidence bytes.

The experiment's parent setup/reporting chat separately consumed at least 2,272,290 tokens in
the saved snapshot. That overhead belongs to the study, not selectively to a comparison arm.
Repeated monitoring and reporting can overwhelm savings on a tiny task.

## Why OpenCode looked better

The successful saved OpenCode hybrid used three cloud assistant messages for planning, one local
dispatch and final review. Its stored cloud categories were input 6,493, output 756 and cacheRead
6,400. Against its own cloud-only control it recorded 47.9% less input and 44.0% less output, at
roughly twice the elapsed time. Those runs are saved in
`~/Projects/opencode-agent-setup/docs/benchmarks/2026-10-22-cloud-led.json`; the earlier audit
`~/benchmark-evidence/matched-20261025/old-opencode-audit.json` reconciled the assistant counters
with the saved SQLite sessions.

The task, effort, local model, harness and counter definitions differ. Do not add OpenCode cache
reads to input as if their relationship were identical to Codex, infer dollar savings, or call
this a matched absolute comparison. The useful pattern is fewer cloud handoffs with complete
review evidence, not a promise that this addon can reproduce those raw totals.

## Repair boundary

The normal handoff should return review evidence inline, followed by one explicit digest-bound
decision and a concise verified closeout. Keep canonical JSON available for machine callers and
all full evidence on disk. Do not remove source/diff coverage, ordinary or independent tests,
semantic approval, scope/permission/citation/native checks, retry deadlines or verified recovery.

The first two local implementation assignments exhausted their 900s and 1,200s budgets. Both
failed drafts were rejected and archived with verified recovery refs/bundles. The user explicitly
authorized one smaller local repair, limited to the CLI, focused tests and skill. The broad draft's
canonical-rendering changes are excluded: they broke legacy views, omitted contract fields and
crashed on an unavailable log. Preserve the existing renderer and acceptance pipeline instead.

The installed helper self-check also attempted a protected `~/.codex` read in the pilot. The
denial is retained. This is not justification to weaken permissions or omit launcher checks.

Full repair history and saved-run response/command accounting are retained outside worktrees at
`~/.local/share/codex-local-worker/review-overhead-fix-20261026`. Offline tests can establish the
new handoff's behavior. Its actual cloud-token savings remain unmeasured.

## Final verification and integration

The smaller local worker completed the CLI/tests/skill patch. All 25 pre-existing functions
outside the CLI entry point remained byte-for-byte unchanged, including render/package/validate/
decide and archive handling. The canonical-rendering changes from the rejected broad draft were
not used.

The launcher-wrapped test run could not complete: Seatbelt integration-probe tests cannot create
a nested sandbox from inside the worker verification sandbox. This failure and the protected
helper read denial remain in the original report. Native automatic acceptance was explicitly
rejected; it was not relabeled successful.

The parent ran the unchanged full suite on the host, where the actual permission probes work:
1,171 tests passed, with the same eight live tests skipped, in 237.1 seconds. No live model tests
were enabled. The same cloud parent semantically reviewed the local patch and bound its explicit
decision to the package, patch and host-verification digests. It integrated those exact reviewed
bytes through git apply, checked each resulting source hash, and retained the native failure
separately. The original targeted suite passed all 45 tests against the integrated checkout; all 17 package
tests also passed. Reviewed source hashes stayed unchanged by these tests. This is a
documented parent integration, not a claim of successful automatic decide acceptance.

The first two interrupted local attempts had unavailable token usage, never zero. The completed
smaller worker recorded 1,461,859 local input tokens (1,399,552 cached subset) and 32,113 output
tokens. These are implementation costs, not a new live comparison or measured cloud saving.

All failed drafts and the completed worker have verified recovery archives. The source of truth
for review, verification and cleanup receipts is the external repair evidence directory above.
Actual token savings for this fix remain unmeasured.
