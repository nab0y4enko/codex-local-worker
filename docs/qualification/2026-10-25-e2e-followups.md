# Remaining end-to-end fixes and qualification (2026-10-25)

Follow-up to [the first re-run](2026-10-25-e2e-cli-rerun.md). The user authorized further
local and cloud qualification tests until the identified issues were addressed. All work uses
the existing `universal-local-worker` checkout, Python 3.9 and the standard library. No push
or main merge is authorized. Dashboard Tasks 27–28 remain deferred.

## Changes

- Added `examples/read-task.json`, tested with the production parser; it includes required
  `goal`, supported read fields and an answer schema. The skill/README link it. Empty source
  scopes are now rejected at contract validation because every read answer requires citations.
- Added a bounded source-citation helper. It emits line numbers including blank lines and
  ready-to-copy exact citation objects, checks resolved repository/scope containment and
  symlink escapes, and bounds file count, bytes, lines and serialized output. The read prompt
  selects relevant files under directory scopes and uses the helper. Final schema/path/range/
  quote validation remains unchanged; the launcher does not silently repair invented citations.
- Added advisory static review of changed Python tests. The worker receives a self-check and
  assertion guidance; report and planner JSON expose `review_findings`, and summaries retain
  only safe finding counts. Unperformed reviews remain unavailable. Unexpected scanner errors
  preserve job outcome/audit/reporting and produce a safe advisory. A diagnostic is not proof
  of test correctness; helper-based tests and other languages still need semantic review.
- Planner usage now includes source, timestamp when present, and snapshot/final completeness.
  A final label needs a fresh token count for the latest started turn plus a matching completion
  marker; missing/active linked children keep totals incomplete. The summary explicitly labels
  active snapshots, withholds a final local-share ratio, and can be refreshed after Codex exits
  without another model call. In-turn evidence must be preserved before refreshing.
- Read answers now expose `answer_evidence`: schema/source-citation checks only, semantic review
  required, commands not independently verified by the launcher. This closes an evidence-labeling
  gap exposed by the negative tests below; it does not make arbitrary model prose infallible.
  The skill requires reviewing source claims and verifying commands before relying on them.

## Test-first and independent review

Behavior regression tests failed before implementation for the new report findings, prompt
requirements, stale planner counts/completeness, missing read example, empty read scope,
assertion omissions, bounded source reads, and advisory error isolation. Tests also cover
scope/path/symlink escapes, blank lines, wrong-but-in-range citation lines, output limits,
linked-child missing usage, safe parse-error diagnostics and unperformed review metadata.

Independent reviews covered the citation helper, test scanner/report integration and
planner completeness. Their findings about directory scopes, stale counts, unperformed reviews
and advisory exception isolation were corrected and regression-tested.

Intermediate full offline runs passed 1,077 tests (363.532 seconds) and 1,079 tests
(355.034 seconds), each with eight live-only skips and no warnings. They are intermediate
checks; later behavior changes require the final run recorded below.
A subsequent 1,080-test run failed in `test_claim_and_publication_are_one_critical_section`
with `JSONDecodeError`: the test parent observed an outcome file during its child writer's
open/truncate-before-payload interval. This was not reported as a pass. Both pool fixture
publishers now stage JSON in a sibling temporary file and atomically replace the public path.
Deterministic regression tests fail on the original direct publication and pass on the fix;
the combined pool checks pass 38/38. No launcher behavior or timeout was weakened. The final
full suite below includes both harness fixes. Subsequent intermediate runs passed 1,081 tests
(372.928 seconds), 1,082 (368.218 seconds), and 1,097 (407.159 seconds), each with eight
live skips and no warnings. The last run overlapped later source changes and is not the final
current-head proof; logs are retained under the sequential run's `offline/` directory.

## Retained negative local read pilots

Evidence roots:

- `~/.local/share/codex-local-worker/followup-qualification-2026-10-25-run1/`
- `~/.local/share/codex-local-worker/followup-qualification-2026-10-25-run2/`

Both use a new installed marketplace plugin, isolated homes/state and a small textkit fixture.
The question asks what `normalize_whitespace` does and which command runs the existing tests.
Each worker used the citation helper successfully through its real read-only permissions.
No cloud inference was used for these two pilots.

| Job | Launcher result | Citation check | Independent semantic result |
| --- | --- | --- | --- |
| `20261025T201754Z-read-helper-pilot-b416b6` | `ok`, one attempt, 100.64 seconds | 4/4 verified | Rejected: answer recommends `python -m unittest tests.test_core`; `python` is unavailable (exit 127). |
| `20261025T202111Z-read-helper-pilot-08de64` | `ok`, one attempt, 87.41 seconds | 4/4 verified | Rejected: repeats the unavailable command despite stronger inference-labeling prompt guidance. |

Pilot 1 tokens: input 70,241 / cached 62,259 / output 2,514. Pilot 2: input 42,550 /
cached 35,269 / output 2,151. Combined local pilot usage: input 112,791 / cached 97,528 /
output 4,665; no cloud planner was invoked for these pilots.
All usage fields are retained in the job reports. These are **not full read-answer successes**. The citations and helper worked; semantic command correctness did not.
The model's inference cannot be made trustworthy by a prompt assertion alone.

Both rejected results were archived with receipts and recovery bundles, preserving reports,
worker event streams and the semantic rejection records. Their worktree directories were removed
by the archive CLI. No user evidence was deleted or overwritten.

After the second failure, the launcher gained the explicit validation-scope metadata described
above. It intentionally does not execute arbitrary generated commands automatically. The cloud
planner remains responsible for semantic acceptance and safe command verification. The final
end-to-end test exercises this planner/worker boundary rather than counting quote validation
alone as proof of a correct answer.

## Additional negative end-to-end run

Evidence: `~/.local/share/codex-local-worker/followup-qualification-2026-10-25-final/`.
Installed marketplace plugin at source `624a00c`; setup applied in an isolated home. Single
prompt requested a local read then the two features. No mid-run intervention.

| Job | Outcome | Input / cached / output tokens |
| --- | --- | --- |
| `20261025T204256Z-inspect-conventions-ed8250` | Local read rejected: only 1/4 manually transcribed citations verified. | 43,225 / 34,793 / 3,134 |
| `20261025T204459Z-inspect-conventions-4d9314` | Explicit cloud read escalation, `ok`; planner reviewed sources after a sandbox refusal. | 64,727 / 47,360 / 677 |
| `20261025T204538Z-add-slug-0bf178` | Local write timed out; planner reviewed its saved patch, normalized trailing whitespace and integrated it. 13 fixture tests passed. | unavailable; missing is not zero |
| `20261025T204539Z-add-stats-28444a` | Local write timed out with three incorrect test expectations; not integrated. | unavailable; missing is not zero |

The planner deliberately used two slots on one inference server. Disjoint files did not mean
independent inference resources. README/skill now explicitly require sequential jobs on a single
server unless concurrent capacity has been qualified. This run is **incomplete**, not a success:
only slugify reached the primary checkout, with the edits disclosed above. The planner archived
both reads and slug job; it preserved the unfinished stats worktree and asked about repair. After
recording that state, the parent rejected and archived stats with a separate manual receipt;
that cleanup is not attributed to the autonomous run. All reports and recovery bundles remain.

Planner rollout final count: input 1,474,757, cached 1,405,696, output 6,075, reasoning 558.
Cloud worker usage is separate above; local totals are incomplete because both writes have no
completed usage record. The in-turn summary remains a snapshot; the post-exit shell refresh
records final planner usage without another model call. The temporary auth copy was deleted.

Source commits: `55646ba` (read evidence/template), `9987687` (test review advisories),
`484476b` (snapshot/final usage), `af9553e` (atomic test fixture publication),
`624a00c` (semantic review), `6499a8d` (source references/concrete interpreter),
`81258f4` (shared inference serialization), `c421b32` (size check before source quote read).

## Citation-reference repair and negative pilot

The failed read motivated a structural repair: local answers return only short `source_id`
references from helper output. The launcher resolves observed IDs into full citations, checks
scope and current source, then applies the original envelope/quote validator. Unknown, forged,
malformed and conflicting references fail closed; identical repeated helper entries deduplicate.
Legacy full citations and cloud output schemas remain compatible. Raw worker output is retained.
A version marker distinguishes helper envelopes from ordinary JSON files with a `files` key;
a red-green regression covers unrelated JSON without weakening citation verification.

The first reference pilot (`20261025T205300Z-read-helper-pilot-599fbd`) was rejected: macOS's
`python3` shim prepended Xcode cache warnings to the helper JSON, so no IDs resolved; the model
also exceeded the 500-character summary bound. It used 36,211 input / 29,432 cached / 1,897 output
tokens. Evidence and archived recovery bundle live under
`~/.local/share/codex-local-worker/followup-qualification-2026-10-25-references/`.
A red-green regression now requires the helper command to use the launcher's concrete
`sys.executable`, avoiding the shim without granting additional write permissions.

The concrete-interpreter pilot `20261025T205457Z-read-direct-python-c53abf` produced clean
helper JSON but repeatedly echoed candidate answers through shell commands instead of finishing.
It timed out after 304.04 worker-seconds with no final answer or completed token record. Result:
`answer_invalid`, answer withheld, usage unavailable. It was rejected and archived separately.
Candidate answer text is not counted as successful local read qualification.

## Final offline and end-to-end qualification

A further intermediate run passed 1,098 tests in 378.375 seconds (eight live skips, no warnings).
The final source-size guard was added after that run started; current-head proof is recorded below.

Sequential evidence root:
`~/.local/share/codex-local-worker/followup-qualification-2026-10-25-sequential/`.
A fresh marketplace installation and setup apply used `codex-home-qualified`; installed source
hashes match `81258f4`. The later `c421b32` change only moves the existing reference source-size/
scope check before quote reading; its red-green regression and final full-suite result are separate
from that installed run. No scope or permissions were relaxed. The single initial prompt explicitly
authorized sequential local retries/repairs, with no parent messages or code edits during the run.

The read job finished with nine verified source references. Slugify passed review and integration.
The first stats result passed its worker tests but failed planner semantic review: its broad Unicode
range included punctuation/symbols and excluded astral letters; an expected dictionary had duplicate
keys, and its independent-call assertion was weak. The planner rejected and archived that result,
then issued a local repair with explicit Unicode, dictionary and independence assertions. This
rejection is retained as part of the run, not relabeled as a successful stats implementation.

| Local job | Launcher / reviewed outcome | Input | Cached input | Output |
| --- | --- | ---: | ---: | ---: |
| `20261025T210038Z-inspect-conventions-67f97f` | `ok`; 9/9 citations verified; source review accepted | 39,166 | 31,134 | 3,220 |
| `20261025T210235Z-add-slugify-16932f` | `ok`; 15 tests; integrated | 44,064 | 37,132 | 2,460 |
| `20261025T210414Z-add-word-frequencies-e05992` | `ok` structurally; 13 tests; rejected in semantic review | 118,581 | 108,485 | 3,933 |
| `20261025T210644Z-repair-word-frequencies-3a5ce1` | `ok`; 16 tests; repaired feature integrated | 161,836 | 149,017 | 6,479 |
| **Local total, including rejected work** | four one-attempt jobs; 524.09 worker-seconds | **363,647** | **325,768** | **16,092** |
| **Cloud planner** | final rollout count; no cloud worker escalation | **2,051,411** | **1,986,688** | **5,776** |

Cloud planner reasoning output: 190 tokens (included in output, not additive). Counts are not a
price comparison: input includes cached tokens, and local/cloud tokens can have different costs.
The in-turn summary correctly labels planner usage a snapshot and withholds final local share.
After process exit, the runner preserved `summary.in-turn.md/json`, refreshed the installed summary
without another model call, and confirmed `completeness: final` with no incomplete linked children.
The cloud `turn.completed` record agrees with the final rollout counts. Final summaries now have
both tier totals populated. `run-receipt.json` confirms exit 0 and auth-copy deletion.

Independent post-exit verification reran **30 fixture tests, all passing**, then checked eight explicit
slug cases, eight explicit word-frequency cases, 200 seeded word-tokenization/reference and slug
idempotence cases, and mutation independence. One initial em-dash slug expectation was wrong:
the written contract discards non-ASCII before replacing separators, so `a—b` correctly becomes
`ab`, not `a-b`. The failed probe expectation and its correction are preserved separately; no
implementation was changed to satisfy it.

The planner made only disclosed integration edits: trailing-whitespace normalization in the repaired
stats files and correction of an inaccurate Unicode comment in its test. Slug files are byte-identical
to their archived worker results. Repaired stats source matches after whitespace normalization;
its test differs by that normalization and the comment correction. The rejected stats patch was not
integrated. No parent intervention, parent implementation, cloud worker or screenshot was involved
in this sequential run.

All four jobs have archive receipts, matching recovery refs, SHA-256-verified bundles and successful
`git bundle verify` results. All worker worktree directories were removed. `git worktree list` contains
only the fixture primary checkout at initial commit `9710c2d`; its four added feature/test files remain
untracked and preserved, with no commit or push. Independent receipts, raw worker streams, contracts,
probe script/results, original in-turn summary and refreshed final summary remain in the evidence root.

Final current-head offline suite at code commit `c421b32`:
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests` → **1,099 tests OK,
8 opt-in live tests skipped**, 373.194 seconds, no warnings. Python 3.9, standard library only.
The complete log is retained as `offline/codex-local-worker-current-head-offline.log` in the
sequential evidence root. No source/test changes followed this run.

## Remaining limits

No identified fix is deferred. Dashboard Tasks 27–28 remain outside this task. Source citation
validation proves quoted source/schema consistency, not arbitrary prose, command correctness or
implementation correctness. Static assertion advisories likewise cannot replace semantic review;
the rejected Unicode implementation demonstrates why. The loaded 9B model can time out or loop,
and missing usage stays unavailable. Those negative runs remain recorded above. Native/live-only
suite cases are still opt-in; the eight standard skips are not reported as live passes.
