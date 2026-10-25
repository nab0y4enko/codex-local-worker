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
  same audited job path with cloud-specific argv and `--escalates` linkage. After review, cloud
  jobs use the selected native `CODEX_HOME` so Codex owns credential locking and refresh; the
  profile permits metadata on that directory, the literal `auth.json`, and observed
  `tmp/arg0-macos` helper scratch only. Initial and refreshed credential values feed redaction,
  and retained text evidence is scrubbed after execution. Local jobs keep isolated homes. No
  `permissions.json` is modified.
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

The spike at `docs/qualification/2026-10-24-cloud-auth-spike.md` records synthetic evidence.
`codex login status` accepts a fabricated fixture under temporary `HOME`/`CODEX_HOME` with the
generated metadata-only home access and scoped auth/helper exceptions. Runtime tests prove native
home selection and redaction across simulated refresh. No real home was read and no cloud request
or model inference was made during this batch. A real token’s validity, refresh locking, and
refresh behavior through a live cloud request remain unverified.

## Commit sequence

- `a125beb feat: classify failures and tell the planner what to do next`
- `7400a67 fix: scope budgets to the task and escalate instead of blocking`
- Task 17 commit recorded in the final response.

## Review fixes

- The classifier no longer consumes raw `stderr_excerpt`. Only extracted errors from documented
  terminal JSON events are eligible; a raw stderr false-positive regression verifies that generic
  infrastructure phrases do not trigger an automatic retry. Classifier fixtures now build their
  typed diagnostics by parsing actual terminal JSON event shapes. Read-job evidence uses the same
  typed extraction.
- Replaced the reviewed auth copy/writeback flow with native Codex auth management. The generated
  deny profile hides selected `CODEX_HOME` child files except literal `auth.json` and observed
  `tmp/arg0-macos`; it grants only `file-read-metadata` on the home directory itself so Codex can
  inspect the directory at startup. A Seatbelt regression confirms directory listing, config,
  and history reads remain denied; profile exceptions outside the hidden home are refused.
- Synthetic CLI probes: `codex login status` exited 0 (`Logged in using ChatGPT`) under the
  generated profile with a synthetic home lacking `config.toml`; the CLI created only the observed
  `tmp/arg0-macos/<random>/.lock` and patch helpers. A second probe,
  `codex exec --ignore-user-config --ignore-rules --ephemeral --json --help`, exited 0 with
  synthetic config and session files present. No inference or cloud request was made. The exact
  real ChatGPT auth refresh lock/path remains unverified. Profile tests establish config/history
  file contents stay denied.
- Review-fix focused commands and results:
  - `python3 -m unittest discover -s tests -p test_cloud_tier.py -v`: 7 passed in 0.839s;
    output: `batch-15-17-review-fix-cloud.log`.
  - `python3 -m unittest discover -s tests -p test_classify.py -v`: 11 passed in 2.882s;
    output: `batch-15-17-review-fix-classify.log`.
  - `python3 -m unittest discover -s tests -p test_permissions.py -v`: 168 passed in 8.638s;
    output: `batch-15-17-review-fix-permissions.log`.
  - `git diff --check`: passed.
- No broad suite rerun was requested for the review fix; the previously recorded full run remains
  the 990-test passing run above.
