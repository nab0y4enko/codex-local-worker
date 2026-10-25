# Task 26 live qualification

Date: 2026-10-25T12:03:58.199711-04:00
Platform: macOS-27.0.1-arm64-arm-64bit arm64
Launcher: `/Users/oleksii.naboichenko/Projects/codex-local-worker/bin/local-worker`
State: `/Users/oleksii.naboichenko/.local/share/codex-local-worker/live-2026-10-25c/qualification/state`
Live mode: `LOCAL_WORKER_LIVE=1`; local jobs used `bin/local-worker run`.
Cloud inference: not called; dry catalog and argv check only
Structured read output: local and cloud read requests use `--output-schema`; the launcher independently validates schema and citations.
Native plan: not supplied; native case skipped

## Discovered local profile

```json
No profile captured.
```

## Case results

| Case | Result | Duration (s) | Input tokens | Cached tokens | Output tokens | Evidence |
|---|---:|---:|---:|---:|---:|---|
| test_write_pilot_ok_inside_wrapper | PASS | 19.964 | 15569 | 14190 | 468 | ok; report=/Users/oleksii.naboichenko/.local/share/codex-local-worker/live-2026-10-25c/qualification/state/reports/20261025T160340Z-pilot-add-fix-c95444/report.json |

Token counts are taken only from launcher reports; unavailable values remain marked.
Durations are measured by this harness around the case.
