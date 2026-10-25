# Task 26 live qualification — OleksiiacStudio.localdomain

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
