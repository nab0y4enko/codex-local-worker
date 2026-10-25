# Second-model qualification — 2026-10-25

A live run of the launcher against a different model than the one recorded in
[2026-10-25 readiness](2026-10-25-readiness.md). Failures are reported as the tool classified
them; nothing was retried until green and no test was altered to pass.

## Setup

| Item | Value |
|---|---|
| Source | branch `universal-local-worker`, after the R25 documentation commit and the live-suite host-name commit |
| Model (canonical id = api model) | `Qwen3.5-9B-OptiQ-4bit` (oMLX 0.7.3.dev1, `vlm` engine) |
| Context window / max output | 262144 / 32768 tokens |
| Throughput reported by oMLX | prefill 302.5 tok/s, generation 44.1 tok/s |
| Thinking default | on; no reasoning-effort selection |
| Fallback | none (`model_fallback: false`), Codex 0.164.0 compatible |
| Earlier model (readiness record) | `Qwen3.8-Flash-Next-UD-Q4_K_XL` / `qwen-3.8-nf-splash`, context 262144 |
| Cloud | not called (`LOCAL_WORKER_LIVE_CLOUD` unset; cloud case is dry argv/catalog only) |

Offline suite before the run: 1025 tests, OK (skipped=8 live opt-ins), 415.5 s.

## Live suite (8 cases, 333.5 s total, 7 pass, 1 fail)

Run with `LOCAL_WORKER_LIVE=1` and the native plan, task and an owned simulator supplied.

| Case | Result | Duration (s) | Tokens in / cached / out | Notes |
|---|---|---:|---|---|
| discovery_matches_server | PASS | 1.96 | n/a | preflight profile matched the server |
| cloud_escalation_argv | PASS | 2.27 | n/a | dry catalog, no inference |
| write_pilot_ok_inside_wrapper | PASS | 24.14 | 12126 / 8844 / 357 | status `ok` |
| forced_capability_failure_offers_escalation | PASS | 69.07 | 20369 / 16154 / 1383 | `tests_failed`, escalation offered, as asserted |
| forced_context_overflow_is_environment | PASS | 13.07 | n/a | classified `environment` / `context_overflow`, status `answer_invalid` |
| native_before_after_screenshots | PASS | 189.64 | 14192 / 10112 / 554 | exact `testAppShowsAfter` passed; before, after and reference images via simctl and xcresult |
| summary_written | PASS | 16.68 | n/a | |
| read_pilot_citations_verified | **FAIL** | 16.63 | 3724 / 0 / 80 | status `answer_invalid`, class `capability`, code `unverified_answer`, next action `escalate_cloud` |

Native case: the repository ships `scripts/prepare_native_live_pilot.py`. I created one iPhone 17
simulator on the iOS 27.0 runtime (`lw-qual-2026-10-25b`), generated the fixture under
`~/.local/share/codex-local-worker/live-2026-10-25b/native`, ran the case, then shut down and
deleted that simulator. No other simulator was touched (two other iPhone simulators were already
booted and stay booted).

### The read failure (model behaviour, not a launcher bug)

The model answered the read pilot without reading the file. The event stream has zero tool calls
and 3724 input tokens (the prompt only). It returned `title: "Render the widget title"` with the
quote `TITLE = "Render the widget title"`; the real line is `TITLE = "Qualification Widget"`. The
launcher rejected it: `citation 0: quote does not match the cited lines`, 0/1 verified, answer
withheld. This is the safeguard working: a schema-valid but invented answer was not released.

The same pattern appeared in the manual read job below (zero tool calls, invented function name and
line numbers). Both read jobs also show the reasoning item and the final message carrying the
identical JSON, which suggests the answer was produced straight away under the `--output-schema`
constraint. That is a hypothesis; I did not inspect oMLX's structured-output handling. The
write job, which has no output schema, used tools normally.

## Two manual jobs (`bin/local-worker run`, throwaway repo)

Repo: `~/.local/share/codex-local-worker/live-2026-10-25b/jobs/repo` (`textutil.py` with
`slugify` and `word_count`, plus a two-test unit file). State dir under the same `jobs/` folder.

| | Write job | Read job |
|---|---|---|
| Task | add `truncate(text, limit)` plus tests, verify with `python3 -m unittest -v` | explain how `slugify` handles whitespace and name the word-counting function, with verbatim citations |
| Status / classification | `ok` / `ok`, next action `done` (exit 0) | `answer_invalid` / `capability` `unverified_answer`, next action `escalate_cloud` (exit 12) |
| Attempts | 1 of max 2 | 1 |
| Duration | 76.9 s job, 74.5 s worker, 0.19 s verification | 10.3 s job, 9.3 s worker |
| Tokens (in / cached / out) | 52449 / 46096 / 2430 | 3716 / 2048 / 153 |
| Verification | passed, 6 tests OK | citation check failed, 0/2 verified (`line range is outside the file`) |
| Scope | changed only `textutil.py` and `test_textutil.py`; no violations; primary unchanged | answer withheld |

Write diff, read by me: correct. `truncate` raises `ValueError` for `limit < 3` first, returns the
text unchanged when `len(text) <= limit`, otherwise `text[:limit-3] + "..."`, matching the contract
(`truncate('abcdefgh', 5) == 'ab...'`). The tests cover exact length, long and invalid limit.
Weakness: the test named `short` uses `("abc", 3)`, which is the same boundary as the exact-length
case; there is no genuinely shorter-than-limit input, and `limit == 3` with a long string is not
tested. Style nit: one blank line between functions, and a docstring that restates the contract.

Read answer, rejected: `slugify` "replaces runs of whitespace with a single underscore" (the real
code joins words with a hyphen), and the counter is `count_words` (the real name is `word_count`),
cited at lines 13 and 41 of an 11-line file. Everything was invented; the launcher caught it.

## Compared with the earlier model (readiness record)

- Earlier record: 8/8 live cases, 165.2 s. This model: 7/8, 333.5 s, but 189.6 s of that is the
  native case and the earlier record does not break out per-case times, so the totals are not a
  like-for-like speed comparison.
- Write path: this model completed the write pilot and the manual write job correctly on the
  first attempt, with verification passing and the diff inside allowed paths. No difference in
  launcher behaviour was observed.
- Read path: the earlier model is recorded as eventually passing the read pilot (with earlier
  citation failures kept in history and withheld). This model fails it twice out of two and does
  not call tools in read jobs at all in this environment. Every classification came from the
  launcher: `capability` / `unverified_answer` with `escalate_cloud`, which is the right
  recommendation for a model that cannot ground its answers.
- Capability failure and context overflow classified the same way as before.
- Prompt caching: large cached-input fractions on write jobs (about 88 percent), none on the
  first read pilot.

## Other work in this session

- R25 (`touch .` at the job worktree root under the deny wrapper) was investigated and **not
  fixed**. In a standalone Seatbelt profile with `(deny file-write* (literal <dir>))`, `touch <dir>`
  stays blocked when any single `file-write-*` operation is re-allowed (times, data, create,
  unlink, mode, owner, flags, xattr, setugid, acl), and when all of those are re-allowed together;
  only `(allow file-write* ...)` unblocks it, which also re-opens rename. Denying any one of those
  operations alone does not block `touch` either. So this macOS does not offer the operation
  split. The limit stays, and the README note now records the experiment. No source change.
- The live suite default record name is now `<date>-host-record.md`, and its title no longer
  contains the host name.

## Limits

- Two small jobs and one fixture each; this shows behaviour on these tasks, not a reliability
  or speed benchmark.
- One run of the live suite, one run per manual job. No repeat, so run-to-run variance for the
  read behaviour is unmeasured beyond the two read jobs (both failed the same way).
- The structured-output hypothesis for the missing tool calls was not tested (for example by
  running a read-like task without `--output-schema`).
- Cloud inference, the dashboard and a second Mac were not exercised.
- Artifacts are retained under `~/.local/share/codex-local-worker/live-2026-10-25b/` outside the
  repository: `qualification/live-report.md`, `live.log`, `jobs/`.
