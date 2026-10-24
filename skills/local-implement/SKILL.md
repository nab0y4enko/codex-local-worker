---
name: local-implement
description: Use when implementing, fixing, refactoring, or testing code in a trusted local Git repository under this setup's pinned local-Qwen worker policy.
---

# Local Implement

The model selected in this Codex window plans, scopes, and reviews. The pinned local Qwen
model performs every implementation and repair; the launcher then runs the contract's
verification commands. Both the worker and verification run full-host: the launcher's own
filesystem permissions, network enabled, no sandbox confinement, no approval prompts, and no
execpolicy rules. This is a local routing default, never a reason to switch to a cloud worker.
It does not change the window model, global provider, or app-wide enforcement.

Resolve the plugin root from this skill's own path instead of trusting a remembered checkout.
This file is `<root>/skills/local-implement/SKILL.md`, so the plugin root is two directories above
it, `plugin.json` sits directly inside it, and the launcher is `<root>/bin/local-implement`. The
root is wherever the installed package lives; it changes on every update or reinstall, so never
hardcode a personal checkout path. The user-wide default that routes work here is
[the canonical global policy](references/global-policy.md).

```sh
<root>/bin/local-implement --help
```

The default execution backend is `full-host-access`; legacy explicit helpers also exist.
The worker runs `codex exec` with
`-s danger-full-access`, `approval_policy=never` and `--ignore-rules`; verification runs the
contract argv arrays directly on the host, unwrapped. Every result records `sandboxed: false`
with the backend and confinement, so a report never claims a sandbox it did not apply. Legacy
confined helpers still exist in the code for an explicitly selected backend; nothing in the
default flow uses them, and no current instruction asks you to generate or grant Seatbelt or
`codex sandbox` rules.

## Before launching

1. Read the repository instructions and enough of its specification to write a narrow,
   reviewable task contract. Keep the primary checkout and its existing changes intact.
2. Run the endpoint/model check:

   ```sh
   <launcher> preflight
   ```

   It confirms the pinned alias, loopback endpoint, availability, and disabled fallback. It
   does not check the native toolchain, simulator, or dependency cache. Stop on failure; do
   not repair account permissions automatically or route to another model.
3. Use one shared default state directory and one worker by default. Raise `--max-workers`
   only for independently scoped contracts with non-overlapping owned paths and a clear
   integration plan. The pool is shared across launcher copies; each Apple simulator UUID
   also has one exclusive lease. See [the operating policy](references/operating-policy.md)
   before changing concurrency, retry, or recovery behavior.
4. Use the stable workstream name `default` unless the task has an established workstream.
   For an ordinary task, write the nine-field contract shown in
   [`examples/task.json`](../../examples/task.json) and run:

   ```sh
   <launcher> run --task-file /absolute/path/task.json --json
   ```

   For a native Apple task, first use the native gate below. `--no-verify` means verification
   was not run; it is not a bypass.

## Native Apple task

The legacy `preflight` checks model routing only. Before Qwen sees a native task, require a
versioned native preflight plan and run:

```sh
<launcher> run --task-file /absolute/path/task.json \
  --apple-simulator-udid <UUID> \
  --native-preflight-plan /absolute/path/native-plan.json \
  --workstream <stable-name> --job-kind implementation
```

The gate must build a fresh app from the selected worktree with the real toolchain, install
and launch it on that UUID, then run the planned positive XCTest target on that same device,
using the same full-host permissions and environment as later verification. Every step and its
artifact path must pass before the worker starts. Missing dependencies, an invalid plan, an
unavailable simulator, a failed app step, or a command that could not run makes the job
`blocked`; stop there and report the check. Availability or metadata alone is never
qualification: require the actual build product, launched bundle identifier with a positive
process ID, and a result bundle with at least one passing test and no failures.
`--preflight-only` runs this gate without starting Qwen. Read
[Apple verification](references/apple-verification.md) for the plan schema and evidence.

A native job stays on the full-host backend: the opt-in pins scope, not confinement. One
selected simulator UUID is held under an exclusive lease and the toolchain environment is
pinned, while `HOME` and `CODEX_HOME` stay unchanged. The worker can call `xcrun`, `simctl`,
`xcodebuild` and `xcresulttool` directly, so keep it bound to the selected device and its task
scope. `--no-verify` cannot bypass this gate for a native task. A runner PID or suspended
process is not a passing XCTest case. For startup hangs or cross-job asset-compiler failures,
use the bounded diagnosis in the Apple reference.

## Repairs and stop conditions

Give every related job the same stable `--workstream` value; a task ID or job ID change does
not start a new repair history. Mark the initial job `--job-kind implementation` and any
follow-up `--job-kind repair`. A repair contract names the exact failed acceptance item,
relevant diagnostic, and narrow paths it may change.

The launcher preserves a per-workstream circuit breaker across jobs. Two consecutive worker
attempts that produce no in-scope patch stop further launch for that workstream, even if a
new task ID is supplied. A repair workstream also has a maximum of two worker attempts and
1,200 seconds aggregate budget shared across all jobs in that workstream, including
concurrent jobs, waiting, and verification. `--preflight-only` starts no worker and does not
consume a policy attempt. The launcher rechecks persisted usage before each start and never
renews or resets it automatically. If a limit is reached, stop and report the preserved
evidence; change the plan only after human review.

Reserve verification time in `timeout_seconds`: the default worker-attempt cap is 300
seconds, and the verification reserve is 20% of the job budget, bounded from 5 to 60
seconds. The worker must not consume the entire job deadline. A zero-patch run, failed
worker, blocked gate, skipped check, or timed-out verification is not success evidence.
Acceptance is the planner's decision after inspecting the patch and actual verification
results.

## Monitor and review

While a worker runs, use the live status command and stream available attempt logs:

```sh
<launcher> status --state-dir "$LOCAL_IMPLEMENT_STATE_DIR" --job-id <job-id>
```

Status reports elapsed time and token usage only when measured, with its source or an
explicit unavailable/partial state. Missing log output is not evidence of a stall; do not
invent a completion estimate or infer a throughput or cost. Use actual state transitions
and emitted events to describe progress.

After completion, inspect `report.json`, `changes.patch`, each attempt's last message and
logs, every native preflight artifact, and every `verify-attempt-N-M.log`. Report actual worker,
preflight, and verification durations plus measured usage by attempt. Keep cached-token
counts separate from uncached counts; never label tokens as billed dollars or present
missing counts as zero. The worktree, report, and patches remain available for review.

Check the audit and each acceptance criterion yourself. A passing test does not excuse an
out-of-scope edit. A worker summary or a readiness probe is not semantic acceptance. Never
apply an unreviewed patch to the primary checkout or auto-merge it. When work is finished,
the reviewed parent follows the global completion rule: commit and push the task branch,
archive needed artifacts and remove completed linked worktrees. Workers never perform those
Git or cleanup operations. If a repair is warranted, use the
same workstream, a new narrowly scoped contract, and `--job-kind repair`; stop at the
circuit-breaker or repair budget.

This skill is discovered from the installed package, so there is nothing to copy here. The
installer behind [setup](../setup/SKILL.md) maintains the small managed global instruction block
that points at this skill and at [the canonical global policy](references/global-policy.md); a
legacy hand-copied `~/.codex/skills/local-implement/SKILL.md` can be retired once the plugin
install is qualified. A worker never installs a skill or writes global configuration.

## Failure handling

- Read the current report's `failure_reason`, gate checks, attempt records, audit, and
  verification logs. Do not diagnose from an old run or quoted text in model output.
- A missing native dependency cache is a preparation blocker, not a code regression. Network is
  already enabled on the full-host backend, so prepare a pinned, worktree-local cache through
  an explicitly authorized deterministic setup step, or report the dependency as blocked.
- For a native failure, distinguish gate failure from build, install, launch, and test failure
  using [Apple verification](references/apple-verification.md). Fix a command or the task
  contract; do not restate an availability check as proof that tests ran.
- Keep active work and evidence. At completion, preserve failed/superseded drafts in recovery
  refs or verified bundles and retain needed artifacts before removing their worktrees. See
  [retention and trust boundaries](references/operating-policy.md).

## Constraints

- Never run the worker through `codex exec` directly. The launcher is what pins local model
  routing, isolates the worktree, audits its changes, and redacts secrets. Nothing else
  constrains a worker that already has full host access.
- Never change the model selected in this window or the global provider to Qwen. No cloud
  fallback or cloud implementation is permitted by this skill.
- Full-host execution is not a boundary. The worktree, the `allowed_paths` contract, and the
  post-run audit are scope and detection evidence; they do not enforce filesystem permissions.
  Treat anything this host can read or write as reachable, and use trusted, scrubbed copies.
- Run long commands in the foreground through `exec_command` and poll the managed session.
  Never detach a test with a raw `&`, `nohup`, or `disown`, and never poll with sleep-and-tail
  loops: a bare background process dies when its tool call ends and leaves an incomplete log.
  The parent's deterministic verifier is the right place for broad test suites.
- Bound exploration. Read the files a contract names once, do not re-inspect unchanged
  evidence, and prefer a compact result manifest over long case tables.
- Only use the launcher with trusted repositories. Its reports are review evidence, not
  approval to integrate changes.
