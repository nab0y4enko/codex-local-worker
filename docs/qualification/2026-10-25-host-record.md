# Task 26 live qualification — this Mac (Mac Studio)

Date: 2026-10-25T05:24:17.826395-04:00
Platform: macOS-27.0.1-arm64-arm-64bit arm64
Launcher: `/Users/oleksii.naboichenko/Projects/codex-local-worker/bin/local-worker`
State: `/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state`
Live mode: `LOCAL_WORKER_LIVE=1`; local jobs used `bin/local-worker run`.
Cloud inference: not called; dry catalog and argv check only
Structured read output: local and cloud read requests use `--output-schema`; the launcher independently validates schema and citations.
Native plan: `/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/native-pilot-attachment/native-plan.json`

## Discovered local profile

```json
{
  "accepts_images": true,
  "api_model": "qwen-3.8-nf-splash",
  "canonical_id": "Qwen3.8-Flash-Next-UD-Q4_K_XL",
  "context_window": 262144,
  "engine_type": "vlm",
  "max_output_tokens": 32768,
  "model_type": "vlm",
  "reasoning_efforts": [
    "low",
    "medium",
    "high",
    "xhigh"
  ],
  "thinking_default": true
}
```

## Case results

| Case | Result | Duration (s) | Input tokens | Cached tokens | Output tokens | Evidence |
|---|---:|---:|---:|---:|---:|---|
| test_cloud_escalation_argv | PASS | 0.134 | unavailable | unavailable | unavailable | catalog=gpt-6.1-sol; dry cloud argv passed native read-profile and output-schema self-check |
| test_discovery_matches_server | PASS | 0.108 | unavailable | unavailable | unavailable | preflight profile matched live server; fallback disabled |
| test_forced_capability_failure_offers_escalation | PASS | 19.704 | 21905 | 17216 | 1615 | tests_failed; report=/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/reports/20261025T092016Z-task26-impossible-e40b6c/report.json |
| test_forced_context_overflow_is_environment | PASS | 1.391 | None | None | None | answer_invalid; report=/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/reports/20261025T092036Z-task26-context-overflow-a66827/report.json |
| test_native_before_after_screenshots | PASS | 206.037 | 104101 | 88384 | 3232 | native xctest, before/after/reference screenshots via simctl and named XCTest attachment in /Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/reports/20261025T092037Z-task26-native-write-c40f67/screenshots/index.md |
| test_read_pilot_citations_verified | PASS | 4.314 | 12075 | 9952 | 367 | ok; report=/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/reports/20261025T092403Z-task26-read-pilot-824fe6/report.json |
| test_summary_written | PASS | 2.477 | unavailable | unavailable | unavailable | summary output persisted under /Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/sessions/task26-20261025-052016 |
| test_write_pilot_ok_inside_wrapper | PASS | 7.609 | 12586 | 9152 | 523 | ok; report=/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-final-qual/state/reports/20261025T092410Z-pilot-add-fix-97757d/report.json |

Token counts are taken only from launcher reports; unavailable values remain marked.
Durations are measured by this harness around the case.

## Read evidence and first-attempt quality

The passing read job's retained Codex event stream contains a completed `command_execution` for
`cat widget.py`, exit code 0, with `TITLE = "Qualification Widget"` in the captured output. The
answer's single citation was verified against that source line. I confirmed this directly from the
retained event stream after the 8/8 run; the committed live test now asserts this actual inspection
event in addition to answer schema and citation verification on future runs.

An earlier read trial using a two-field answer contract was withheld: launcher status
`answer_invalid`, citation verification 1/2 because one quote did not match its cited lines; the
classification was capability / unverified answer / escalation. It took 5.52 worker seconds and
reported 12,153 input, 9,984 cached input, and 548 output tokens. Evidence:
`/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/full-live-committed/state/reports/20261025T091550Z-task26-read-pilot-6bb6a4/report.json`.
This failed first trial is retained in the qualification history; the final one-field read pilot
passed 1/1 verified citation. Earlier prototype runs with both citations verified but an overly
strict harness answer assertion are not counted as model citation failures.

## Snapshot and retained failed attempts

The 8/8 live suite ran with runtime commits `dd809b1` and `da74db4`; the test harness and qualification
record were still uncommitted at that point. After the run, matcher hardening was committed as
`77bf117`; it accepts only exact XCTest attachment names or Xcode's observed indexed UUID suffix,
and refuses ambiguous distinct matches. Focused tests cover the observed suffix and false-prefix /
ambiguous cases. No model/native rerun was needed for this matcher-only tightening.

Earlier native capture attempts are retained, not counted as passed qualification: jobs
`20261025T090649Z-task26-native-write-096cd9` and
`20261025T090906Z-task26-native-write-6d141b` passed XCTest but capture failed because result
bundle/install paths did not match the retargeting contract. Job
`20261025T091730Z-task26-native-write-b5f5e1` passed the native gate and simctl sources but did not
export the XCTest attachment because Xcode 27 uses a lowercase manifest filename. These fixture /
export issues were corrected; the final native job passed XCTest 1/1 and captured before, after,
and reference images through simctl and the named XCTest attachment.

## Review follow-up: assert the requested native output

The prior full 8/8 live run predates this hardening and is not evidence for the new `After`
assertion. The native fixture now has two separate tests: the readiness/capture test asserts the
label exists and is used for both baseline/reference capture, while the contract's independent
verification selects only `NativePilotUITests/testAppShowsAfter`, which asserts
`label.label == "After"`. The generated native plan keeps its selector on the readiness test;
the generated task selects the After assertion. The live harness checks the exact task selector
and passing verification record before qualifying screenshots.

The targeted hardened local-worker job passed: `20261025T093743Z-task26-native-write-913ffa`,
status `ok`, total launcher time 110.49s, one local worker attempt 16.21s with 23,280 input,
18,496 cached input, and 1,216 output tokens. The independent `xcodebuild` verification selected
`NativePilotUITests/NativePilotUITests/testAppShowsAfter`, passed with return code 0 in 12.86s.
The full log contains `-[NativePilotUITests.NativePilotUITests testAppShowsAfter] passed
(6.248 seconds)`. `xcresulttool get test-results tests` on the retained result reports
`NativePilotUITests/testAppShowsAfter()` Passed on iPhone 17 Simulator, iOS 27.0. Its result bundle
is copied and hash-verified at
`/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/native-after-assert-v2-qual/state/reports/20261025T093743Z-task26-native-write-913ffa/verification-artifacts/strict-after.xcresult`.
The original bundle also remains inside the job worktree until parent archival. The base-gate
result bundle separately shows `testAppShowsQualificationLabel` Passed 1/1; before/after/reference
screenshots and simctl/XCTest attachments were captured as before.

An initial focused attempt, `20261025T093723Z-task26-native-write-8fb9fd`, stopped before model
execution because native-plan action parsing consumed `test` after an inline `-only-testing:`
selector. The parser now recognizes inline `-only-testing:` and `-skip-testing:` flags without
consuming the following action. A regression preserves the scheme-named-`test` safeguard while
accepting selectors before the real action. The generated fixture puts selectors after `test`,
and the real hardened job passed with that command form.

An actual no-inference `codex sandbox` probe verified the generated native profile using a worktree
path containing spaces, a double quote, non-ASCII `ü`, and a literal `[literal]` directory. It
allowed the worktree read, denied a configured secret read and launcher-state read, and denied a
worktree write; the source remained unchanged. This exposed that direct absolute `read` entries
are parsed as globs when a path has brackets. The launcher now reopens the task worktree via the
exact Codex `:workspace_roots` scope beneath the denied state root while keeping the filesystem
read-only profile and explicit state/secret denials. The CLI smoke record is
`/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/native-profile-awkward-root/profile-cli-smoke.json`.

Focused offline validation after both fixes: 70 tests across worker isolation, read jobs, native
readiness, and the native fixture contract passed in 5.473s. The full 8-case live suite was not
repeated; the targeted hardened native case and no-inference profile checks provide the added
evidence.

## Review follow-up 2: literal brackets in denied paths

The first workspace-root fix still emitted launcher-state and configured secret denials as direct
filesystem entries. The reviewer reproduced that Codex interpreted brackets in those paths as
glob syntax and allowed reads. The profile now represents every denied absolute path as a nested
exact-root rule (`"<absolute path>" = { "." = "deny" }`), while keeping the read-only
`:workspace_roots` exception for the selected worktree.

An actual no-inference CLI regression used launcher-generated `-c` overrides and isolated
`HOME`/`CODEX_HOME`. The synthetic state directory was `state [literal]`, the denied file was
`secret [literal] file.txt`, and the worktree/source also had literal brackets. Codex allowed
reading the selected worktree source, returned `Operation not permitted` for both denied reads,
returned `Operation not permitted` for a worktree write, and the source stayed unchanged. Exact
argv, outputs and timings are retained at
`/Users/oleksii.naboichenko/.local/share/codex-local-worker/task26-live-2026-10-25/bracket-deny-cli-regression/actual-cli-regression.json`.

Offline RED/GREEN checks covered both ordinary and bracketed denial serialization. Focused
validation after this correction ran `python3 -m unittest tests.test_worker_isolation
tests.test_read_jobs -v`: 33 tests passed in 5.336s. No live inference or native build was rerun.
