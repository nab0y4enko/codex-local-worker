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
6. **Summarize.** After the last job, run `<root>/bin/local-worker summary`. Report the generated
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
its worktree. After reviewed work is committed and pushed, archive a completed launcher worktree
with `<root>/bin/local-worker-archive archive --repo <repo> --state-dir <state> --job-id <id>`;
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
