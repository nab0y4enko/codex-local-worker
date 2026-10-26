# Reducing cloud coordination without removing review

The launcher can now run a sequential queue through its existing deadline, local retry,
verification and audit machinery. It stops at cloud semantic review. It never treats passing
checks, an empty advisory list, or a worker's prose as permission to accept a patch.

## Run to review

The normal path is one complete bounded local assignment, planned and reviewed inside the same
cloud window. Put related stages into that one contract instead of buying extra coordination
turns:

```sh
<root>/bin/local-worker orchestrate --task-file /absolute/path/task.json --state-dir /absolute/path/state
```

A declared queue stays supported for independent tasks, with the same guarantees:

```json
{"tasks": ["/absolute/path/task.json"]}
```

```sh
<root>/bin/local-worker orchestrate --manifest /absolute/path/queue.json --state-dir /absolute/path/state
```

Both entries validate and snapshot the contracts, use one inference slot, and record a durable
receipt containing completed jobs and pending tasks. Each job retains its original attempt and
time budget. A failed, incomplete, or timed-out job stops the queue without silently resetting a
workstream, escalating to cloud, or deleting evidence. The command returns only review-package
paths and digests plus status; no model polling is needed. Provider options `--settings` and
`--codex-bin` are forwarded to the normal launcher.

Each job returns a canonical `package.json` and a complete readable `review.md`. Read the view
once: it reproduces the frozen contract, full diff, changed sources, actual checks, findings,
audit and screenshots verbatim, keeping blank lines, literal escapes and code fences exactly. The
view is not hashed into the package; it is verified byte-for-byte against a fresh rendering of the
digest-bound canonical package before any decision, and read at most one byte past the expected
length, so an edited, truncated or oversized view fails closed instead of misleading review.

The canonical `package.json` includes the full bounded patch, changed source files,
actual checks, static advisories, audit, screenshot references, task contract, and hashes of
retained evidence. Large/missing evidence is explicitly incomplete and cannot be accepted by
this command. Full original logs remain linked. An older report without its original contract
cannot be upgraded into current acceptance proof.

For an existing current-format job:

```sh
<root>/bin/local-worker review-package --state-dir /absolute/path/state --job-id JOB_ID
```

A separate read job is optional. Use it when its findings affect planning or will be reused.
For an already-scoped small change, let the write worker read the relevant repository files.
Returned read answers still require citation validation and cloud review of claims/commands.

## Independent acceptance checks

A write contract may add `"acceptance_checks": "/absolute/trusted/checks.json"` (include
`"kind": "write"` when extending a legacy contract). The manifest format is:

```json
{
  "version": 1,
  "checks": [{
    "name": "unicode-and-independence",
    "argv": ["/absolute/python3", "/absolute/trusted/check_words.py"],
    "scripts": ["/absolute/trusted/check_words.py"]
  }]
}
```

The planner supplies these checks before the job. Keep the manifest and every declared script
outside the primary checkout and launcher state; worker-owned test files are not independent
checks. Pin every external script/data dependency that can change the verdict (each listed
script must appear as an argv item). Inline checks may use an empty `scripts` list. Use a concrete
interpreter and trusted dependencies; the launcher does not infer a transitive dependency graph.
The check's working directory is the candidate checkout. It must explicitly load the candidate
code from that directory when an external script's default import path would otherwise omit it.

The launcher pins file identities/hashes and denies writes to these inputs through the existing
permission wrapper. It refuses missing protection, `--no-verify`, changed inputs, and definitions
inside worker-writable locations. Checks use the existing bounded verifier and shared job deadline.
A failure returns concrete diagnostics to the existing local repair loop; no repair may change
these trusted tests or reset the original deadline/attempt cap. Missing checks stay blocked.

This protects check definitions, not their mathematical sufficiency. Candidate code can still be
wrong, and tests may be incomplete. Cloud semantic review remains required. The duplicate literal
dictionary-key advisory catches overwritten expected values cheaply without executing test code.

## Explicit cloud decision

After inspecting the diff, source, tests, findings, audit and screenshots (where applicable), the
cloud planner writes a decision JSON. `reviewer` and `semantic_review` are explicit caller
attestations; this CLI does not authenticate a model identity or perform the review itself.
The reviewer is the same cloud planner session that planned the assignment: there is no noop cloud
preparation session, no mandatory second cloud reviewer, and no model polling between the two
commands. All existing gates stay: checks, semantic sections, permission-wrapped integration,
budgets and verified archive.

```json
{
  "action": "accept",
  "package_sha256": "COPY_THE_EXACT_RETURNED_DIGEST",
  "reviewer": "cloud-planner",
  "semantic_review": true,
  "reviewed_sections": ["diff", "sources", "tests", "findings", "audit", "screenshots"],
  "rationale": "Describe what was checked and why the behavior satisfies the request.",
  "combined_commands": [["/absolute/python3", "-m", "unittest", "discover", "-s", "tests"]]
}
```

```sh
<root>/bin/local-worker decide --state-dir /absolute/path/state --job-id JOB_ID --decision /absolute/path/decision.json
```

The digest binds the report, patch, sources and available evidence. The primary HEAD/index/dirty
contents and worker checkout must still match the review snapshot. Overlapping user edits block
integration. Original verification commands always rerun; `combined_commands` adds checks and
cannot replace them. Trusted acceptance checks also rerun against the integrated checkout.

Integration applies the exact reviewed patch, with no automatic whitespace edits. A whitespace
error requires a corrected candidate and a new review. Verification uses a probed permission
wrapper that makes primary source read-only and protects state/trusted inputs. Tests requiring
writable build artifacts must place those artifacts outside the primary checkout; failures are
reported, never bypassed. Without an enforceable wrapper, automatic acceptance fails closed.

After checks pass, the existing archive code verifies its recovery ref, bundle, and receipt before
removing the worker checkout. The command writes the session summary without inference. The
summary may still be an active planner snapshot; refresh after planner exit to obtain final counts.
No commit, push, or merge is performed.

On a failed integration check, applied changes and the worker checkout remain available, with an
`integration_failed` receipt. No automatic reset or rollback is attempted. A crash leaves an
`accepting` receipt; inspect it before manual recovery. If archive is blocked, status becomes
`accepted_archive_pending` (or `rejected_archive_pending`); finish archive using the existing
archive CLI after active jobs exit. Replaying a decision never reapplies a patch.

Use `"action": "reject"` with an explicit rationale to archive a reviewed, rejected result without
integration. Rejecting does not require successful checks, but still requires unchanged evidence.
Never-started jobs without a worker checkout retain their ordinary reports and need no archive.

Queue jobs are independent candidates. Accepting one changes the primary snapshot and invalidates
other packages made against the earlier state. Regenerate and review those packages before
accepting them. Dependent changes should be one coherent job or planned after an earlier acceptance.

## Measure the result

`local_share` describes token placement, not savings. The benchmark command compares matched
accepted tasks with final cloud cached input, uncached input and output counted separately. It
includes failed/repaired worker usage and planner review, refuses incomplete/mismatched evidence,
and performs no inference or pricing conversion. See `examples/benchmark.json` and
`local-worker benchmark --help` for the evidence manifest.

The code and offline tests establish workflow behavior. They do not establish a measured reduction
in cloud tokens. That requires real paired direct-cloud / launcher runs with the same starting
source, task and acceptance requirements; direct Luna can be an additional comparison arm.
