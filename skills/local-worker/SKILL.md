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

## Planner loop

Follow these steps for each repository request:

1. **Decompose.** Turn the request into small, ordered jobs with clear acceptance criteria and
   disjoint `allowed_paths` for write jobs. Use a read job first when repository behavior or
   facts need inspection before implementation. Keep the user's main checkout intact.
2. **Read job.** Create a `kind: "read"` contract with a question, `scope_paths`, an
   `answer_schema`, and an answer-size limit. The launcher validates the JSON answer and checks
   its citations against the worktree. Review the answer and the cited files before planning
   edits.
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
   without a worktree need no archive; retain their reports.

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
