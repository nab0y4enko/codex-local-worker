# Launcher operating policy

Read this when planning concurrent jobs, diagnosing the native gate, handling
repairs, or deciding whether old evidence can be removed. It describes the local launcher
policy and its limits; the CLI help and job report are authoritative for a particular run.

## Local model and execution boundary

The launcher pins `qwen-3.8-nf-splash` through `local-omlx` at
`http://127.0.0.1:8000/v1`, requires fallback disabled, and self-checks worker argv before
launch. It does not load the interactive Codex configuration. The selected window model
remains the planner and reviewer. No alternate or cloud model is allowed when local
preflight or worker launch fails.

Execution is full-host for both halves of a job. The worker runs `codex exec` with
`-s danger-full-access`, `approval_policy=never`, and `--ignore-rules`, so no approval prompt
and no execpolicy rule gates it and its network access is the launcher's own. Verification runs
the contract argv arrays directly on the host, unwrapped, and each result records
`sandboxed: false` with the backend and confinement actually applied. A command that could not
run is reported `blocked` with the reason it was not run; a command that ran and exited non-zero
is reported `failed`. Never relabel one as the other.

The worker's worktree is the working directory, not a security boundary. The primary checkout is
fingerprinted before and after, and the worktree is audited against the base commit, but Git
worktree isolation and the post-run audit do not enforce filesystem permissions: they detect
out-of-scope work after the fact. Treat everything this host can read or write as reachable by a
job, keep secrets out of the repository, and use trusted, scrubbed repository copies.

Reports and patches are evidence, not semantic review. Workers must not apply, merge, reset,
commit, push or prune output. The parent reviews acceptance and follows the user’s global
completion policy: commit and push the task branch, then remove completed linked worktrees
after preserving necessary history and artifacts. This is not authorization to merge
unreviewed worker patches or discard unrelated changes.

## Pool, workstreams, and repair bounds

Use one state directory for the pool; separate launcher copies do not create separate
capacity. Default concurrency is one. The optional ceiling of up to four workers only applies
to independent, mergeable contracts with disjoint owned paths in a shared pool. Same-repo
path overlaps are refused; an Apple simulator UUID also has one exclusive lease. Multiple
workers share the same local oMLX endpoint and do not promise higher throughput.

Keep a stable workstream name across implementation and repairs. A task ID or job ID change
does not reset its persisted circuit breaker. Two consecutive worker attempts with no
in-scope patch stop future launches in that workstream. A repair workstream is limited to two
worker attempts and 1,200 seconds aggregate job budget; the launcher rechecks persisted usage
and does not renew the budget automatically. Preserve state across process restarts and do
not remove the circuit or budget record to make a retry available. The semantic decision to
accept a patch remains with the planner.

Each job's `timeout_seconds` is a single budget for lock waiting, endpoint preflight, native
gate, worker attempts, verification, audit, and reporting. Reserve sufficient time for
verification and reporting before forming the worker deadline. A worker consuming the full
budget leaves no valid verification evidence. The final report must distinguish actual
elapsed worker, gate, verification, and total durations.

## Live progress and usage evidence

Use `status --state-dir <dir> [--job-id <id>]` to read active progress and actual elapsed
time. The command may report usage while the job is active or after it completes. Every
usage value must include its source and completeness; mark unavailable fields as unknown and
partial values as partial. Do not convert missing counts to zero, infer tokens per second,
estimate a dollar bill, or suggest a countdown. Do not call an empty log a stall: log output
may be buffered until an event or process completion. Stream logs when available and use
actual process/event state for progress updates.

At completion, report token usage for every attempt with its source. Keep cached and uncached
counts distinct. Report worker, native gate, each verification command, and total wall-clock
time from measured timestamps. An absent model event field is unknown routing metadata, not
proof of a fallback; local routing is established by pinned request configuration,
self-check, and endpoint preflight.

## Process ownership and evidence discipline

Run every long command in the foreground through the agent's managed exec session and poll that
session until it exits. Never detach a build or test with a bare `&`, `nohup`, or `disown`, and
never poll with sleep-and-tail loops: a process that is not owned by the tool call may be
terminated before completion, leaving truncated logs and an unknown outcome. Broad suites belong in the
parent's deterministic verification pass, where each command gets its own bounded slice of the
single job budget and its own log. Keep reads targeted: read a named file once, do not
re-inspect unchanged evidence, and report a compact result manifest instead of a long per-case
table.

## Native Apple execution

Native work is opt-in and stays on the full-host backend. `--apple-simulator-udid` pins scope,
not confinement: the selected tvOS simulator UUID is held by one exclusive pool lease, the
toolchain environment is pinned, `HOME` and `CODEX_HOME` stay untouched, and
`IBToolNeverDeque=1` is forced so a pooled `ibtoold` cannot reuse another job's state. No
Seatbelt profile is generated, no confined readiness probe runs, and no scratch confinement is
configured; the report names `full-host-access` as the backend and records that the native probe
was not run, because the gate is the positive check.

The worker has the launcher's own permissions, so `xcrun`, `simctl`, `xcodebuild`, and
`xcresulttool` are available to its own tools directly against the selected device. The contract,
not a profile, bounds that access: stay on the selected device, never boot, erase, or touch
another device, and never wait on a device another job leased. Launcher-run verification and the
native gate use the same full-host environment.

Qualification is positive and actual. The gate must build a fresh app from the job worktree,
install it, launch it under the planned bundle identifier with a positive process ID, and run the
planned XCTest target on that same simulator, producing a result bundle with at least one passing
test and no failures. Each stage records its command, duration, exit status, and artifact path; a
missing dependency, unavailable device, invalid plan, or failed stage blocks the worker launch.
Availability and actual test evidence are distinct claims, and neither is a confinement claim.

Prepare dependencies before relying on a cold build. A pinned, worktree-local source package
cache keeps a build reproducible: verify `Package.resolved` pins against checkout revisions,
avoid symlinks, hardlinks, or Git alternates back to the primary checkout, rewrite embedded
absolute paths, preserve read-only files, and record source, destination, revisions, and manifest
hash. Stage it after the worktree exists. Do not copy a prior app product or imply the launcher
prepared a cache.

For a nested SwiftPM macro build that fails with `sandbox_apply: Operation not permitted` and a
malformed `SwiftMacros.TaskLocalMacro` response, passing
`OTHER_SWIFT_FLAGS=$(inherited) -Xfrontend -disable-sandbox`,
`-IDEPackageSupportDisableManifestSandbox=YES`, and `-IDEDisablePackageManifestCaching=YES` for
that command addresses the Xcode-internal compiler and manifest sandboxes. Confirm
`-disable-sandbox` with the installed `swift-frontend -help-hidden`. Put an explicit result
bundle under report-owned scratch or an already gitignored worktree path; never widen
`allowed_paths` just for build output.

### Historical: confined Seatbelt and workspace-write backends

Older jobs ran native verification inside
`/usr/bin/sandbox-exec -f <report-dir>/apple-verify.sb` with a generated deny-default profile:
writable roots limited to the job worktree, report-owned scratch, one validated simulator device
directory, fixed Apple log/cache directories, and an OS-derived per-user temp directory; denials
for the primary checkout, worktree `.git`, `.agents`, `.codex`, and the resolved Git directory
after the allow list; TCP/UDP denied including loopback with one exact literal outbound exception
to the selected simulator's `testmanagerd` Unix socket; Mach access limited to fixed named
services plus anchored CoreSimulator and `com.apple.dt` patterns; and conjunction-bound
pseudo-terminal grants for `/dev/ptmx` and `^/dev/ttys[0-9]+$`. Ordinary verification could also
run under the legacy `codex sandbox -P local-worker-verify` profile with network disabled.

Both are legacy compatibility paths, reachable only when a caller explicitly selects the
`apple-seatbelt` or `codex-workspace-write` backend. They are not the default, not required for a
native job, and not something a current task should generate or grant rules for. Their historical
probe evidence — granted scratch write, refused outside write, refused loopback connect — is
confinement metadata from those backends only and is not acceptance or qualification evidence for a
full-host job. Retained toolchain, plan-schema, cache, and result-bundle guidance above still
applies.

## Failure classification

- A quoted unsupported-tool message in task text, logs, or model chat is data. Only launcher
  failure-node diagnostics can establish a routing failure; a real routing failure stops the
  job without changing models.
- A missing dependency cache is a preparation blocker, not a code regression.
- An unavailable selected simulator is a device-selection/state blocker; do not rerun the
  same worker contract.
- A stale guard after an intentional behavior change needs its semantics updated, not
  weakened or deleted.
- A baseline command failure is pre-existing only when shown on the unchanged base commit.
  Compare the preserved worktree and prior verification evidence before assigning cause.
- An XCTest startup or harness error before any case executes is an environment failure, not a
  test failure. Confirm it in the staged diagnostics, record that no case ran, and never score
  it as a passing or failing test.
- Under full-host execution there is no profile grant to tune: classify a native failure from
  the actual command output and result bundle, then fix the command, the plan, or the
  environment.

## Completion and retention

The reviewed parent commits and pushes finished task changes to their branch before reporting
completion, without another permission prompt. Verify the remote branch. Do not force-push or
merge a different branch without authorization. Workers never perform these completion steps.

Leave no linked worktrees for completed task work. Inventory `git worktree list`, state
worktrees, dirty files and active jobs. Keep the primary checkout and unrelated active work.
Preserve detached commits on recovery refs and in a verified Git bundle; preserve needed
uncommitted drafts in recovery commits or archives. Move needed ignored native artifacts,
logs, result bundles and reports outside worktrees before removing them. Native managed
worktrees use recoverable archival; legacy unmanaged trees use Git removal after preservation.
No cleanup may discard the only copy of work or evidence. A missing remote or unsafe cleanup
is an explicit completion blocker, not grounds to claim completion or delete first.


## Native startup qualification

Runner creation, a startup PID, or a successful build does not qualify XCTest. A spawned process
can remain stopped at dyld while the harness waits for its startup handshake. Preserve staged
`Session-*.log` diagnostics and require positive hosted and UI test counts from a complete result
bundle on the selected simulator. Use a bounded diagnostic run instead of repeating the default
startup wait for every bundle. Disabling the scheme debugger does not remove this host's
process-control dependency; do not modify the app scheme or omit native test bundles to avoid
qualifying the harness. `IBToolNeverDeque=1` stays forced so a pooled asset compiler cannot reuse
another job's state; qualify repeat fresh builds, not only the first warm run. Historical notes on
the confined backend's exact Mach-service names and PTY rules are in
[Apple verification](apple-verification.md). Only a newly evidenced, scoped failure justifies
another fix.
