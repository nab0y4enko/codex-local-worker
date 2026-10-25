# Tasks 15–17 implementation report

Branch: `universal-local-worker`

## Changes

- Task 15 (`a125beb`): classify failures from launcher evidence and status, never worker prose;
  citation verification failures become `unverified_answer`. The launcher performs bounded
  safe retries once per failure code, preserves the verification reserve, and emits the stable
  §11 planner summary. Regression coverage exercises two distinct safe fixes in one job and
  refuses to repeat either failure code.
- Task 16 (`7400a67`): scope breaker and repair counters to local workstream/tier and keep cloud
  allowance independent. Capability failures alone increment the streak; environment failures
  reset it. Legacy local state migrates without refunding budgets; corrupt or active legacy state
  fails closed. Existing locking, reservation checks, and repair limits remain covered. Explicit
  reset records the actor.
- Task 17: expose the live `codex debug models` catalog and route cloud escalation through the
  same audited job path with cloud-specific argv and `--escalates` linkage. Cloud jobs use a
  private per-job `CODEX_HOME` containing only a safe copy of the selected home’s `auth.json`.
  A state-local account lock serializes launcher jobs; source hashes guard atomic refresh
  writeback. Source/destination symlinks, non-regular files, and concurrent source changes are
  refused. Newly observed auth secrets are added to redaction and retained text evidence is
  scrubbed before the final report is persisted. No `permissions.json` is modified.
- Untyped failed writes with no surviving patch now count as capability `no_change`; writes
  failing after a surviving patch count as `tests_failed`; a failed read without a valid answer
  counts as `invalid_answer`. Typed infrastructure evidence takes precedence.

## Verification

- Task 15 focused checks: classifier 9, pipeline 15, run-job 18 passed.
- Task 16 focused checks: policy 11, classifier 9, pipeline 15, run-job 18 passed. The policy
  tests cover local legacy migration/corruption, active reservations, lock-based admission,
  environment streak reset, and independent cloud budgets.
- Task 17 focused checks: cloud tier 9 passed, including synthetic auth copy/redaction,
  refresh writeback, changed-source refusal, launcher concurrency refusal, and symlink refusal.
- Integration rerun: discovery, hardening-job, classifier, policy, and cloud suites; 107 tests
  passed in 14.996s.
- First full run, exec session `98081`: 989 tests in 120.797s; 4 errors and 1 failure. The
  discovery CLI assertions used the pre-§11 verbose output shape, and a policy regression used
  a generic failed worker exit while expecting a capability failure. The CLI assertions now
  check the stable summary fields; the classifier now uses launcher-observed job outcome for
  untyped failed exits without misclassifying them as environment failures.
- Final full run, exec session `68067`: `python3 -m unittest discover -s tests -v`; 990 tests in
  118.527s, all passed. Full output is in
  `batch-15-17-full-suite-final.log`; the first run is preserved in
  `batch-15-17-full-suite.log`.
- `git diff --check` passed before the final commit.

## Auth evidence and limits

The spike at `docs/qualification/2026-10-24-cloud-auth-spike.md` records synthetic evidence:
`codex login status` accepted a fabricated fixture under temporary `HOME`/`CODEX_HOME`, and
runtime tests prove copy, refresh writeback, conflict refusal, and redaction behavior. No real
home was read and no cloud request or model inference was made during this batch. A real token’s
validity and refresh behavior through a live cloud request remain unverified.

## Commit sequence

- `a125beb feat: classify failures and tell the planner what to do next`
- `7400a67 fix: scope budgets to the task and escalate instead of blocking`
- Task 17 commit recorded in the final response.
