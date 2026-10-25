# Handoff: universal local worker (2026-10-25, live qualification in progress)

## Objective and instructions

User: **Finish all, test on the real local model, analyse results and fix all issues; fully ready tool.**
Continue with **gpt-6-luna subagents**, explicitly requested instead of historical Sonnet/local-worker routing.
Use the current primary checkout on `universal-local-worker`; no extra planner worktrees.
Commit reviewed changes and push this task branch under the current user completion rule; no force push or main-branch merge.

## Recovery map

- Spec: `docs/superpowers/specs/2026-10-24-universal-local-worker-design.md`.
- Plan: `docs/superpowers/plans/2026-10-24-universal-local-worker.md`.
- Current ignored ledger: `.superpowers/sdd/2026-10-24-universal-local-worker/progress.md`.
- Briefs, reports, reviews and logs live beside the ledger. Do not force-add this temporary workspace to Git.
- Original ignored ledger/reviews R1–R25 were missing at resume. Their wording cannot be recovered or invented.
  The current ledger reconstructs completed work from the earlier handoff and commits, then records this continuation.
- Tasks **1–25 complete and reviewed**. Do not redispatch them. Reviewed product code through `b9484c4`;
  `3830bd7` removes accidentally tracked temporary evidence while keeping local copies. Those changes are pushed.
- Package version **0.3.0**; first-party manifest `.codex-plugin/plugin.json`.
- Latest full offline run: **960 tests, OK, 135.916s**. Later focused checks 85/21 and archive-install check passed.
  The install check verifies exact archived manifest version plus all three skills using disposable HOME/CODEX_HOME.

## Current work: Task 26

Luna implementer `task26_live` is building/running live qualification and fixing observed failures.
Its report and compact jobs manifest will be in the ignored plan workspace; read them before further work.
Supporting native plan: `native-live-pilot-plan.md`. Read-only dashboard investigation: `dashboard-readiness.md`.

- oMLX app was started; actual model discovered/loaded: `Qwen3.8-Flash-Next-UD-Q4_K_XL`,
  alias `qwen-3.8-nf-splash`, context 262144, fallback disabled. Rediscover live; do not hardcode this into product code.
- Own qualification root: `~/.local/share/codex-local-worker/task26-live-2026-10-25`.
- Own iOS simulator: `24033911-49F7-4D8F-ADDD-233ECB533393`. Two unrelated booted tvOS devices must remain untouched.
- First native plan had an invalid result-bundle path; corrected to report-owned `apple-scratch`.
- Native fixture built/installed/launched with positive PID, but XCTest failed because SDK27.1 requires UIScene lifecycle.
  Fixture generator was corrected with SceneDelegate. Keep failed jobs/results; never claim this was a pass.
- Actual impossible-test local job classified `tests_failed -> escalate_cloud` correctly:
  24565 input / 19200 cached / 2598 output, 27.64 worker seconds. Reasoning count not reported.
- A 4096 metadata-cap case did not force server overflow: server still has its discovered larger context.
  Genuine overflow test must exceed discovered server window, or explicitly report the plan assumption mismatch;
  never classify model narration as a typed environment error.
- An async request for **one minimal real cloud smoke call is unanswered**. No cloud inference is authorized yet.
  Local inference is explicitly authorized. Synthetic cloud authentication tests already passed; real refresh remains unverified.

## Remaining sequence

1. Finish Task26: real read/write/environment/capability/summary cases; real native build, test and images where feasible;
   analyse/fix failures; review actual images; full offline suite and scoped review. Commit durable qualification record.
2. Archive/remove ONLY owned completed live-job worktrees after review and task-branch push; keep verified bundles,
   receipts, reports and required artifacts outside removed worktrees. Manifest must include failed/superseded jobs.
3. Tasks27–28: desktop MCP Apps spike first. Rendering/modes/pip/update behavior remain unproven.
   Do not mark GO from static code or standard protocol support. Task28 is conditional on an observed GO.
   The old approval gate before installing into the real desktop remains relevant; prepare a concrete reversible spike first.
4. Final whole-branch review and fixes, final checks/install qualification, commit/push, archive/cleanup, final report.
   No history squash: T2 commits were already published before this continuation; do not force-rewrite them.

## Final-review carry items

- Historical Task9: root timestamp handling, writable Git objects, gitlink reporting, packed-refs.lock noise.
- Check orphan process-group tracking/reaping for native/capture phases before the worker starts, especially SIGKILL.
- Cloud authentication uses Codex-managed actual cloud CODEX_HOME, no plugin copy/writeback; profile grants only
  home metadata, literal auth.json, and observed tmp/arg0-macos helper subtree. Local jobs retain isolated homes.
- Capture checkout commands are wrapped and deadline-bound; all source simulator locks are sorted and acquired
  after global worker lock; overlay ancestors are strict; artifact roots are preopened under apple-scratch.
- Preserve all new `Ruling:` lines from the ledger in final reporting; do not invent missing original rulings.

## Standing boundaries

No AI attribution in Conventional Commits. Never print the oMLX key or read ~/.ssh, real Git configuration,
or oMLX settings values. Discovery may handle credentials programmatically without exposing them.
Live worker runs use isolated homes, no cloud calls without approval, no real global-home migration during qualification.
Do not delete unrelated work, jobs, simulators, or user evidence. A failed/skipped/blocked check is not success.
