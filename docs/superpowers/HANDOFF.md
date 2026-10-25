# Handoff: universal local worker (2026-10-25, core qualified; desktop gate pending)

## Objective and instructions

User: **Finish all, test on the real local model, analyse results and fix all issues; fully ready tool.**
Continue with **gpt-6-luna subagents**, explicitly requested instead of historical Sonnet/local-worker routing.
Use the current primary checkout on `universal-local-worker`; no extra planner worktrees.
Commit reviewed changes and push this task branch under the current user completion rule; no force push or main-branch merge.

## Recovery map

- Spec: `docs/superpowers/specs/2026-10-24-universal-local-worker-design.md`.
- Plan: `docs/superpowers/plans/2026-10-24-universal-local-worker.md` (28 tasks).
- Current ignored ledger and all temporary briefs/reviews/logs: `.superpowers/sdd/2026-10-24-universal-local-worker/`.
  Never force-add this workspace. Durable evidence is under `docs/qualification/`.
- Original ignored R1–R25 ledger was absent at resume; do not invent its wording.
  Recovered continuation rulings are preserved in `docs/qualification/2026-10-25-decisions.md`.
- Tasks **1–26 complete and reviewed** through `0777c63`, including two Task26 fix/re-review rounds.
  Package version **0.3.0**, sole manifest `.codex-plugin/plugin.json`.
- Reviewed core/source evidence through `eb77c06` and the prepared dashboard spike pushed to `origin/universal-local-worker`.
  No published history rewrite, main merge, or force-push.

## Actual local qualification and completed cleanup

- Discovered/loaded oMLX model: `Qwen3.8-Flash-Next-UD-Q4_K_XL`, alias
  `qwen-3.8-nf-splash`, context262144, fallbackfalse. Rediscover live; never hardcode it.
- Task26 real launcher suite **8/8 in241.783s**; offline977total/8live-only skips, OK138.237s.
  Actual write and independently cited read pilots passed. Earlier read1/2citations was correctly
  withheld and retained; model reliability is not perfect. Genuine context overflow was typed
 893955tokens>262144, not inferred from model narration or metadata cap.
- Actual Xcode27.1/iOS27 UIKit native pilot built/installed/launched/tested and captured six
  nonuniform images. The separate strict `testAppShowsAfter` passed1/1; baseline readiness alone
  cannot qualify a no-op as success. Parent inspected images and queried copied XCResult.
- Read jobs use one strict Codex native named profile extending `:read-only`; trusted parent
  has no outer wrapper, stdio MCP does. Explicit state/secret denials use nested exact-root
  dot rules, not glob keys. Actual bracket/unicode path, secret/state/read/write probes passed.
- External evidence: `~/.local/share/codex-local-worker/task26-live-2026-10-25`.
  ALL39owned jobs archived via launcher and independently verified; source linked worktrees
  removed,952799349ignoredbytes preserved,0blocked/skipped.29fixture primary repos retained.
  Durable cleanup record `docs/qualification/2026-10-25-cleanup.md`.
- Own old simulator24033911-49F7-4D8F-ADDD-233ECB533393 shut down/deleted.
  Preserve the two unrelated booted tvOS devices. New qualification needs a new owned simulator.

## Completed core readiness fix wave

Broad frozen-core audits A/B/C found6Important+1Minor; consolidated fix wave was completed by Luna
`core_fixwave`. Read `core-findings.md`, `core-fixwave-report.md`, and scoped `core-rereview-*.md`
in the ignored ledger folder before continuing. Root must not stage agent source concurrently.

- A1: per-job private object store with primary readonly alternate, isolated hash/collision
  validation before parent reads, validated promotion during archive. No shared object writes.
- A2: gitlinks remain untraversed, now always explicitly unaudited+violating, never silently clean.
- B1: durable-before-release supervisor keeper with OS birth identity/SID/PGID and all-stage
  tracker; uncertain ownership preserved; signal escalation revalidates identity.
- B2: one absolute deadline through compatibility, discovery, rechecks and diagnostic probes.
- C1/C2/minor: first-try metric requires one clean attempt; typed sandbox_denied with safe
  remediation, no raw-stderr classification; permission history correctly labelled global.
- Final code `7465273`; evidence `eb77c06`. All consolidated core findings and scoped
  follow-ups addressed; A/C re-reviews clean, B final scoped re-review clean39focusedtests.
- Final offline **1013total,1005executed successfully,8live-only skips,OK214.097s**.
  Final real local **8/8 PASS165.160s** on `7465273`, including strictAfterXCTest and captures.
  Exact-HEAD isolated package install/three-skill discovery passed; source remains unchanged.
- All10NEWjobs archived via committedCLI and independently verified, linked worktrees removed,
 274492701ignoredbytes preserved. Across allphases49jobs/1227292050ignoredbytes retained.
 8newfixtureprimaryrepos retained as recovery bases. Old39archives untouched.
- New owned iPhone simulator4D3B2328-F90D-4703-9D83-956C7AFE5083 shutdown/deleted;
  both unrelated tvOS devices remain booted. Immutable review snapshot archived/hashverified
  and removed from/tmp. No owned job worktrees remain.
- New evidence/manifest: `~/.local/share/codex-local-worker/core-fixwave-2026-10-25/`.
  See `docs/qualification/2026-10-25-core-fixes.md`, `2026-10-25-cleanup.md`,
  `2026-10-25-readiness.md`, `2026-10-25-usage.md` and recovered decisions.
- Source/evidence through `eb77c06` pushed; final readiness/cleanup/usage/decision records live on this task branch.

## Dashboard gate and remaining sequence

- Task27 prepared/reviewed v0.1.2 spike,7protocoltests plus actual isolated CLI app-server
  startup/tool-list, no inference. Package `/tmp/codex-dashboard-mcpapps-spike-20261025`;
  plugin `dashboard-mcpapps-spike-20261025@dashboard-spike-20261025`, distinct from real plugin.
  Synthetic fixed fixture only, no environment override/symlink escapes. Scope re-review clean.
- Real desktop **NOT installed/tested**; GO/NO-GO undecided, Task28 not started.
  Explicit async question allowing temporary desktop installation/test/removal is UNANSWERED.
  Do not infer a failed capability from missing authorization. Do not restart app or mutate
  real home without authorization. See `docs/qualification/2026-10-25-dashboard-spike.md`.
- Separate one-minimal-cloud-smoke async request also UNANSWERED. No launcher cloud inference.
  Cloud authentication/catalog tested synthetically; actual refresh remains unverified.
- Core review, final offline/live/install checks, push and owned-worktree cleanup are complete.
- If desktop test approved, observe rendering/hostmodes/PiP/mountedrefresh and decide GO/NO-GO;
  Task28 only after observedGO (or explicit user choice to keep CLI-only). Never fabricate GO.
- Durable readiness/usage/decisions records distinguish actual/synthetic/skipped/unapproved checks.
  The whole28-task plan remains incomplete only at the approval-gated desktop27/conditional28 scope.
  If no approval arrives, report this gate candidly; do not claim the dashboard is qualified.

## Standing boundaries

No AI attribution in Conventional Commits. Never print the oMLX key or read ~/.ssh, real Git configuration,
or oMLX settings values. Discovery may handle credentials programmatically without exposing them.
Live worker runs use isolated homes, no cloud calls without approval, no real global-home migration during qualification.
Do not delete unrelated work, jobs, simulators, or user evidence. A failed/skipped/blocked check is not success.
