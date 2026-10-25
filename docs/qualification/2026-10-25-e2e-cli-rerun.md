# Codex CLI end-to-end re-run (2026-10-25)

Single approved re-run of the cloud planner delegating to the local oMLX worker through the
installed plugin. Tested source: `9f5cc28` on `universal-local-worker`, plugin 0.3.0,
Python 3.9.6, Codex CLI 0.164.0. Dashboard Tasks 27–28 remain deferred.

## Fixes and offline verification

| Finding | Change | Commit |
| --- | --- | --- |
| Lock contention misclassified as verification failure | Typed `WorkerBusyError`, `worker_busy` status and exit 14; retry-local classification with wait/lock-timeout remediation. Pool-unavailable refusals use the same status. | `c3d9ca4` |
| Serialization undocumented | Skill and README say default concurrency is one, queue sequentially or use `--lock-timeout`; deliberate independent concurrency can use `--max-workers` up to four. | `9f5cc28` |
| Job worktrees left behind | Archive each finished job worktree once reviewed and integrated or rejected, including uncommitted integration; retain never-started reports without trying to archive a nonexistent worktree. | `9f5cc28`, `973d7af` |
| Never-started jobs poison token totals | Aggregate totals exclude explicit zero-attempt rows, retaining individual outcomes; missing usage for attempted jobs remains unavailable. | `920a656` |
| Patch whitespace | Document reviewed `git apply --check --whitespace=fix` / `git apply --whitespace=fix`, final diff and combined tests, and disclosure of planner edits including normalization. No launcher whitespace rewrite was added. | `9f5cc28` |

Regression tests were run before implementation and failed with the original classification,
lock exception status, and unavailable aggregate totals. Focused classifier tests then passed
18/18 and ledger tests 13/13. An additional synthetic full `run_job` check with a real held
lock confirmed `worker_busy`, `retry_local`, exit 14, no attempts, no tests and no worktree.
The first sandboxed process-control check also timed out in an existing process-group test;
that sandboxed check was not counted as qualification.

The full host-capable offline command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Result: **1,034 total, 1,026 executed successfully, 8 live-only skips, OK, 342.051 seconds**,
exit 0, no warnings in the log. Evidence: `offline-suite.log` under the evidence root below.
Two independent reviews inspected code and documentation; no code correctness issues
were found. The documentation review's no-worktree archiving ambiguity was corrected.

## Setup and command

Evidence root: `~/.local/share/codex-local-worker/e2e-cli-rerun-2026-10-25/` (`E` below).
New fixture repository: `$E/project`, initial commit `2fc6ae8` on `main`, with the existing
`normalize_whitespace` utility and one unittest. Git identity was supplied explicitly; test
processes used `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`.

Installed through the actual marketplace flow with `CODEX_HOME=$E/codex-home`:

```sh
codex plugin marketplace add /Users/oleksii.naboichenko/Projects/codex-local-worker
codex plugin add codex-local-worker@codex-local-worker
python3 <installed-root>/src/plugin_setup.py --apply
```

Setup created an empty throwaway `AGENTS.md` (no pointer requested). The installed runtime and
changed documentation matched the checkout byte-for-byte; hashes are in
`installation-check.json`. Preflight discovered `Qwen3.5-9B-OptiQ-4bit`, context 262144,
`http://127.0.0.1:8000/v1`, no fallback, and CLI compatibility OK. No model override was used
for the cloud planner.

The single prompt, also retained as `prompt.txt`:

> Add two features to textkit: (1) a slugify(text) function in textkit/slug.py with tests,
> (2) a word_frequencies(text) function in textkit/stats.py with tests. Use local workers
> for the implementation.

```sh
CODEX_HOME=$E/codex-home LOCAL_WORKER_STATE_DIR=$E/state \
  codex exec --json --cd $E/project -s danger-full-access --skip-git-repo-check "<prompt>" \
  > $E/codex-run.log 2> $E/codex-run.stderr
```

Cloud auth was copied with `install -m 600 ~/.codex/auth.json $E/codex-home/auth.json` without
reading its contents. The runner removes that copy in `finally`. No real Codex home was edited.
No manual guidance was sent during the run and no second cloud run was started.

## Re-run result

**Implementation passed with recorded issues.** The single `codex exec` ran from
19:24:19 to 19:31:51 UTC (452.317 seconds), exit 0. No second attempt or mid-run human guidance.
The auth copy is absent, confirmed independently and in `run-receipt.json`.

The planner discovered the installed skill, rejected a failed read answer, ran the two write
jobs sequentially, reviewed both patches/audits, integrated them without a commit, disclosed
its edits, archived every job worktree, and generated the summary. There was no write-worker
lock refusal. `worker_busy` and zero-attempt exclusion were therefore covered by offline and
synthetic integration checks, not reproduced by contention in this live re-run.

### Every local job

All jobs used `Qwen3.5-9B-OptiQ-4bit`, one attempt each, with no audit violations. The write
jobs each reported `primary_unchanged: true`.

| Job | Kind / outcome | Verification | Input | Cached input | Output | Job seconds |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `20261025T192451Z-textkit-inspect-498971` | Read / `answer_invalid`, `unverified_answer` | 1/3 citations verified; answer rejected | 17,274 | 14,367 | 1,060 | 36.27 |
| `20261025T192539Z-textkit-slug-d9819d` | Write / `ok` | 12 unittest cases passed | 141,290 | 128,748 | 5,815 | 172.31 |
| `20261025T192838Z-textkit-stats-043746` | Write / `ok` | 16 unittest cases passed | 96,910 | 84,529 | 5,867 | 170.11 |

The two rejected read-contract invocations before the read job were `contract_invalid`:
`max_attempts` is not accepted for reads, then required `goal` was missing. They did not
start workers or create additional job reports. An initial exploratory `rg` also exited 1
because no matching read examples were found. These are recorded friction, not successful checks.

Both write workers corrected their own Unicode test expectations during their single attempts.
The planner then found and added two missing assertions in `tests/test_stats.py`, corrected
Unicode-casefolding comments, and normalized trailing whitespace using the documented apply flags.
It disclosed these modifications in both progress and final messages. The original worker tests
were therefore not accepted unchanged; passing worker verification alone did not prove their
assertion quality. Independent comparison against archived files confirms slug files and stats
implementation differ only by whitespace; the stats test file also contains the disclosed edits.

The combined project suite passed **27 tests**, both during the planner run and independently
afterwards (`independent-tests.log`, exit 0). No trailing whitespace remains in the fixture's
Python files. The independent check did not modify the fixture implementation or tests.

### Worktree and archive state

An early attempt to archive the slug job while the stats worker was active returned `blocked`,
exit 3, because the shared worker lock was held. It preserved the worktree. The planner retried
once the workers finished and both patches were integrated; all three jobs ultimately received
`archived` receipts, including the rejected read job. The planner verified all three recovery bundles.

Independent checks in `independent-verification.json` confirm for every archive: the receipt
says `archived`, the source worktree is absent, the recovery ref matches the receipt, the bundle
SHA-256 matches, and `git bundle verify` succeeds. Reports, logs, patch files, archive copies,
receipts and recovery refs are retained.

At the end, `git worktree list --porcelain` contains only `$E/project` on `main` at its initial
commit `2fc6ae8`; `$E/state/worktrees/` is empty. The fixture has exactly four untracked integrated
files: `textkit/slug.py`, `textkit/stats.py`, `tests/test_slug.py`, `tests/test_stats.py`.
No tracked fixture file was modified, and no commit or push was requested or performed there.
No post-run manual archiving was needed.

### Tokens

| Tier | Input | Cached input | Output | Reasoning output | Input + output |
| --- | ---: | ---: | ---: | ---: | ---: |
| All local jobs, including rejected read | 255,474 | 227,644 | 12,742 | not reported | 268,216 |
| Cloud planner, complete final turn | 1,799,318 | 1,725,952 | 6,156 | 361 | 1,805,474 |

Cached input is a subset of input, not additional usage. Reasoning output is not added again
to output. Local job wall time totals 378.69 seconds; measured worker-process time is 373.74
seconds. Cloud uncached input is 73,366 tokens. No dollar estimate is made.

The local numbers come from all three job reports and match the summary's populated tier and
model totals. The complete cloud numbers come from `turn.completed.usage` and agree with the
final rollout token count. The planner's summary was written before its final message: it shows
1,696,988 input, 1,629,440 cached, 5,669 output, 354 reasoning, total 1,702,657. It is a mid-turn
snapshot, lower than final usage by 102,330 input and 487 output; it is not the complete planner
total. These figures describe this one re-run, excluding the surrounding development/review chat.

## Remaining issues and qualification limits

- Read-contract discovery remains awkward: the planner guessed an unsupported field and omitted
  a required field before obtaining a valid read contract. The optional read worker then failed
  citation verification. Neither the invalid contracts nor that read answer count as success.
- Worker-generated tests still require semantic review: the frequency worker left two assertion
  omissions that the planner repaired and disclosed. Final integrated tests passed independently.
- Archive and worker activity share a lock. The early archive refusal recovered without human
  help. After this re-run, the documentation was clarified to require an idle shared worker state
  when archiving (finish the queue, or archive before starting the next job). That final wording
  was not installed in this run; no additional cloud re-run was authorized or performed.
- Raw worker patches still contain whitespace; the selected fix is documented integration with
  `--whitespace=fix`, not automatic launcher rewriting. Git emitted whitespace-fix notices during
  integration; they are not warnings from the offline suite, whose log is clean.
- Planner summary usage remains a mid-turn snapshot. Complete final cloud usage must be taken
  from the event stream/rollout as above. No new plugin suggestion was emitted by this run.
- This was one CLI run only. Desktop rendering, alternate planner sandboxes, concurrent worker
  pools, other cloud models, live cloud-worker escalation, and deferred dashboard Tasks 27–28
  were not qualified. No further inference run was made to improve the result.

The five requested fixes are implemented and verified within the limits above. The rejected read
answer and the planner's manual repairs are explicitly retained as issues, not promoted to a
fully successful delegation result.
