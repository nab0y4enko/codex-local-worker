---
name: local-worker
description: Use when a repository task can be split into local read, write, or capture jobs that need verified results, screenshots, or a live cloud escalation path.
---

# Local Worker

Use the window model to plan and review. The launcher discovers the currently loaded local
chat model and its endpoint, pins that profile for each job, and returns structured evidence.
Do not assume a model name, alias, port, or context size.

Resolve the installed root from this skill path: this file is
`<root>/skills/local-worker/SKILL.md`; the root is two directories above it. The manifest is
`<root>/.codex-plugin/plugin.json` and the launcher is `<root>/bin/local-worker`.

```sh
<root>/bin/local-worker doctor --json
<root>/bin/local-worker preflight
<root>/bin/local-worker models --json
```

`doctor` reports Codex compatibility, live oMLX discovery, permission mode and deny lists,
wrapper availability, available simulators, state directory, and plugin version. `preflight`
checks the installed Codex CLI and live local model endpoint. `models` shows discovered local
models and their limits. Treat unavailable discovery as an environment problem; do not guess a
model or endpoint.
`doctor` and `models` are optional diagnostics only: the launcher discovers and pins the model on
every job, so a normal `orchestrate --review` run needs neither call.

## Prefer deterministic coordination

For scoped work, prefer one complete bounded assignment in the calling cloud window:
`<root>/bin/local-worker orchestrate --task-file <absolute-task.json> --state-dir <state> --review`. Put
related stages in that single contract instead of spending extra cloud turns on coordination.
`--manifest <absolute-queue.json>` (legacy, `{"tasks": ["/absolute/task.json"]}`) stays supported
for independent tasks. Both pin the contract, run sequentially through existing verification and
bounded retries, and return package paths, digests and a readable `review.md`. There is no noop
cloud preparation session and no
mandatory second cloud reviewer: the planner that planned the assignment is the reviewer.

Pass `--review` on the normal run: it prints the complete evidence for every job inline in one
response — orchestration status, the whole validated `review.md` view verbatim, the exact bytes of
the bound trusted acceptance manifest and scripts and of each bound verification log (each rehashed
against its package binding), and the ready-to-edit decision command with its digest-bound template.
Inline evidence is never approval: you still make one explicit digest-bound decision, and you can
create `decision.json` and invoke `decide` in the same shell call. A successful closeout reports
`next_action: "done"` only for an accepted or rejected job whose archive was actually verified, so a
verified job needs no canonical-package, log, receipt or summary rereads. Anything else — a blocked
job, `next_action` asking for follow-up, a `TRUNCATED` marker, or an inline-evidence refusal that
names a file — is a real follow-up: read exactly that file and act on it. Without `--review` the
command keeps returning the compact JSON receipt summary.

Cloud semantic review remains mandatory: read the inline review's diff, sources, tests, findings,
audit and screenshots, then post `local-worker decide --job-id <id> --decision
<absolute-decision.json>` with an explicit cloud decision bound to `package_sha256`. This command
validates unchanged review inputs, integrates exact bytes, reruns original/independent/combined
checks, verifies archive recovery, and summarizes. It never creates semantic approval itself.
Incomplete evidence, stale review, missing protection or failed checks block acceptance. Preserve
failure receipts; never clear them to bypass a failed gate. See
[the workflow guide](../../docs/qualification/cloud-token-workflow.md) for the exact decision schema.

Supply independent acceptance checks for predictable edge cases before implementation using the
optional write-contract `acceptance_checks` manifest. Keep definitions outside worker-writable
locations, pin script dependencies, and retain the normal tests and semantic review. Never edit
trusted checks to make a candidate pass. Use conditional reads and coherent bounded jobs to reduce
handoffs. Compare final matched runs with `local-worker benchmark`; local share alone is not savings.

## Planner loop

Follow these steps for each repository request:

1. **Decompose.** Turn the request into small, ordered jobs with clear acceptance criteria and
   disjoint `allowed_paths` for write jobs. Use a read job first when repository behavior or
   facts need inspection before implementation. Keep the user's main checkout intact.
2. **Optional read job.** Skip a separate read when the change is already scoped; let the write
   worker inspect relevant files itself. Use a read when findings affect planning or will be reused.
   Create a `kind: "read"` contract with a question, `scope_paths`, an
   `answer_schema`, and an answer-size limit. The launcher validates the JSON answer and checks
   its citations against the worktree. Review the answer and the cited files before planning
   edits. Start from [`examples/read-task.json`](../../examples/read-task.json); read contracts require `goal` and
   do not accept `max_attempts`. Adjust the repository, existing scope paths, question and schema.
3. **Plan, then write job.** Use a `kind: "write"` contract with a concrete goal,
   `acceptance_criteria`, `allowed_paths`, argv-array `test_commands`, and one total
   `timeout_seconds` budget. The nine-field [`examples/task.json`](../../examples/task.json)
   remains a supported v1 write contract; new contracts may also include v2 fields such as
   `kind`, `workstream`, `environment`, `tier`, and `ui_review`.

   ```sh
   <root>/bin/local-worker run --task-file /absolute/path/task.json --json
   ```

   Jobs run **one at a time by default**. Queue jobs sequentially, or pass
   `--lock-timeout <seconds>` to wait; lock waiting consumes the contract's total time budget.
   A `worker_busy` result means the worker never started: wait and retry as directed, without
   treating it as verification failure. Use `--max-workers 2` (up to 4) only for deliberately
   independent jobs with non-overlapping paths and resources; keep one shared state directory.
   A single oMLX inference server is a shared resource even when files do not overlap. Keep
   its jobs sequential unless concurrent inference capacity has been independently qualified.

4. **Review.** Read the JSON fields `status`, `classification`, `answer`, `patch_path`,
   `screenshots`, `tests`, `tokens`, `durations`, and `report_path`. Inspect the patch, test
   results, audit, and report yourself before integrating anything. For UI work, include a
   native plan and request `ui_review`; inspect the `screenshots/index.md` and each before/after
   image. A `kind: "capture"` contract uses a `capture` object to produce screenshot evidence
   without worker changes; use it only for an explicit capture request, not as a substitute for
   reviewing a write job.
5. **Follow `next_action`.** `done` ends that job. For `retry_local`, apply the concrete
   remediation and retry at most once per failure code, within the original job deadline. Then
   follow the final classification; do not blindly repeat a retry, create a replacement job, or
   reset the workstream. `needs_user` means report the blocker and ask for what is actually
   missing. For `escalate_cloud`, inspect the live catalog with `models --cloud --json`, choose
   the cheapest suitable model from its current descriptions, and start with low reasoning
   effort. Escalate in order: local worker, cheapest suitable cloud model, a stronger cloud
   model only when justified, then planner implementation as a last resort. Each escalation
   must be authorized by the user's request and applicable policy. Continue the same scoped task
   and link it with `--escalates <job-id>`; do not invent a model slug, retry a cloud job as
   local, or silently reset the workstream.
6. **Archive and summarize.** Once each finished job's result is reviewed and integrated into
   the primary checkout, or explicitly rejected, archive its launcher worktree with
   `<root>/bin/local-worker-archive archive --repo <repo> --state-dir <state> --job-id <id>`.
   This applies even when integration is uncommitted; no commit or push is required. Verify
   the archived receipt, recovery ref, and bundle before moving on. Preserve unfinished jobs
   and their evidence, and report any worktree that could not be archived. Never-started jobs
   without a worktree need no archive; retain their reports. Archive only when no jobs are
   active in the shared state: finish the queue first, or archive before starting the next job.

   **Summarize.** After the last job, run `<root>/bin/local-worker summary`. Report the generated
   summary, job outcomes, measured token and time totals, screenshots, escalations, missing
   evidence, and any section named **Suggested fixes for this plugin**. The summary notification
   is a hand-off that the session record is ready; it is not approval to integrate a patch.

## Permissions and recovery

Workers use the launcher's full-host access and a macOS deny wrapper when available. Worktree
scope and post-run audit detect out-of-scope edits; they do not confine filesystem or network
access. Keep secrets out of repositories. `permissions.json` belongs to the user: **never edit permissions.json**.
Read it with `<root>/bin/local-worker permissions show`; use the explicit
`permissions deny-read`, `allow-read`, `deny-write`, or `allow-write` commands only when the user
asks to change the list. Doctor is read-only and does not create a missing permission file.

Use `status --state-dir <dir> [--job-id <id>]` to inspect active work. A failed, interrupted, or
superseded job preserves its report, logs, worktree, and patch for review. Do not reset or merge
its worktree. After a finished job is reviewed and integrated or rejected, archive its launcher
worktree if present (no commit or push required) with `<root>/bin/local-worker-archive archive --repo <repo> --state-dir <state> --job-id <id>`;
verify the recovery ref, bundle, and archive receipt before cleanup. See
[operating policy](references/operating-policy.md) for retention and failure handling and
[Apple verification](references/apple-verification.md) for simulator plans.

## Guardrails

- Never call `codex exec` directly for a job; use the launcher so its discovery, isolation,
  verification, audit, and report stay connected.
- Never hand-apply a worker patch before reviewing the diff and verification evidence.
- A runnable command that exits nonzero is `failed`; a command that could not run is `blocked`.
  Do not present either as a passing check.
- The local model identity comes from live discovery and pinned request evidence. If discovery
  fails, report its classification and follow the returned `next_action`.

### Patch integration

After reviewing a worker patch, use `git apply --check --whitespace=fix <patch>` followed by
`git apply --whitespace=fix <patch>` to correct trailing whitespace during integration. Inspect
`git diff --check` and the resulting files, then run the combined project tests. Disclose any
planner edits to worker results, including whitespace normalization, in the final report.
Summary token totals exclude jobs with zero worker attempts; usage missing from a job that ran
remains unavailable, never zero. Individual job rows retain never-started outcomes.

### Review evidence and final usage

Inspect `review_findings` in each job's JSON/report before accepting its patch. The launcher
performs an advisory static review of changed Python test files for missing direct assertions.
The worker receives the same self-check command. Inspect every finding: helper-based tests may
be valid, and an empty diagnostic list does not prove correctness. For every language, verify
that tests assert observable behavior or expected exceptions and that they would catch a broken
implementation. Repair missing checks and rerun relevant tests; disclose planner edits.

Read workers are given `src/source_citations.py` to obtain numbered source and ready-to-copy
citation entries within the declared scope. Local workers return only the helper's `source_id`
from `source_refs`; the launcher resolves IDs from observed command output and rechecks the
source. Cloud reads retain full citation objects. Never invent IDs, line numbers or quotes. This helper improves evidence capture; final path, range and quote
validation remains mandatory, and an invalid read answer stays withheld. A read `ok` means
those structural checks passed. Its `answer_evidence` explicitly requires semantic review and
does not claim independent command verification. Check the claims against sources and verify
any command before relying on it; absent source commands must be reported as not specified,
and inferred suggestions must be labeled unverified.

A summary generated inside an active planner turn is an explicitly labeled **snapshot**. Its
planner usage and any linked child usage can still grow; local share remains unavailable until
completion is evidenced. To refresh final usage without another model call, preserve the
in-turn summary as evidence and run the same summary command from a shell after Codex exits:

```sh
CODEX_HOME=<planner-home> LOCAL_WORKER_STATE_DIR=<state> \
  <root>/bin/local-worker summary --session <planner-thread-id>
```

The refreshed summary rereads cumulative rollout counts and labels them final only when the
latest matching task-completion markers and usage are present for the planner and linked
children. Missing measurements remain unavailable. Do not call an in-turn summary final or
silently replace retained evidence.
