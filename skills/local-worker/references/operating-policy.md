# Operating policy

Use this reference when a job fails, needs repair or escalation, uses a simulator, or needs
recovery. The current task contract and its report are the source of truth for one run.

## Discovery and permissions

The launcher discovers the active local chat model, its supported limits, the loopback endpoint,
and fallback state at preflight and again before attempts. It does not hardcode model IDs, aliases,
context windows, or a service port. The endpoint must resolve to loopback; port `8000` is used only
when the settings file omits a port. The API key is kept in the child environment and redacted from
argv, prompts, logs, reports, notifications, and the ledger.

Run `<root>/bin/local-worker doctor --json` for a read-only Codex/oMLX/permissions/simulator
snapshot. It never creates a missing `permissions.json`; if absent, it reports in-memory defaults.
`permissions.json` belongs to the user. **Never edit it directly.** Use `permissions show` to read
the effective mode and deny lists. A user-requested change goes through the explicit
`deny-read`, `allow-read`, `deny-write`, or `allow-write` command.

Worker and verification processes use full-host access. The deny wrapper applies the user's short
deny list when available; wrapper availability is reported and can be checked by doctor. A Git
worktree, `allowed_paths`, and post-run audit help scope and detect edits; they do not confine
filesystem or network access. Treat all host-readable files as reachable by the worker, and keep
secrets out of repositories given to jobs.

## Jobs, pool, and repairs

Keep one shared state directory so launcher copies share locks, leases, budgets, and history.
Concurrency is one by default. The optional pool of up to four jobs is for independent contracts
whose allowed paths do not overlap. A simulator UUID has one exclusive lease; separate worktrees
do not make two overlapping patches safe to merge.

The contract's `timeout_seconds` is the total budget for lock waiting, model discovery, native
preflight, worker attempts, verification, audit, and reporting. Reserve enough time for
verification. A command that ran and returned nonzero is `failed`; a command that could not run is
`blocked`. Missing output is not proof of a stall; use launcher events and process state.

Keep the same `workstream` through repairs. The capability circuit breaker and repair budget are
persistent across job IDs. Only the launcher classification and report evidence determine the
next step:

- `done`: review and accept or decline the result.
- `retry_local`: apply the concrete remediation and retry within the one-retry rule.
- `needs_user`: tell the user what environment or input is missing.
- `escalate_cloud`: inspect `<root>/bin/local-worker models --cloud --json`, select the cheapest
  model whose live description fits the task, start with low reasoning effort, and continue the
  same contract using `tier: {"cloud": "<live-slug>"}` and `--escalates <job-id>`.

Do not hardcode a cloud catalog entry or elevate reasoning by default. A second failed repair must
not reset a budget or breaker to permit another attempt.

## Review and recovery

Read the report, patch, verification logs, audit, screenshots, and classification. A passing test
does not replace review of the diff or acceptance criteria. Keep partial and failed job evidence;
do not reset or merge its worktree automatically.

Use `status --state-dir <dir> [--job-id <id>]` for live progress and measured usage. Keep token
counts with their source and completeness; cached counts stay separate. Never turn missing usage
into zero or estimate dollars from token totals.

Once each finished job is reviewed and integrated or rejected, archive its launcher worktree
regardless of whether integration is committed or pushed, with
`<root>/bin/local-worker-archive archive --repo <repo> --state-dir <state> --job-id <id>`. Confirm
the `archived` receipt, recovery ref, and verified bundle/archive copy before cleanup. Preserve
needed logs, screenshots, result bundles, ignored artifacts, and unfinished drafts. Do not delete
unrelated active worktrees.

Run `<root>/bin/local-worker summary` after the session's last job. It writes Markdown and JSON
summaries, sends a best-effort notification, and reports job results, measured totals, screenshots,
escalations, planner usage when available, and **Suggested fixes for this plugin**. Show missing
data as unavailable; the notification is a hand-off, not a review decision.

For native Apple job diagnosis and screenshot evidence, see
[Apple verification](apple-verification.md).
