# codex-local-worker

Actual source repository: <https://github.com/nab0y4enko/codex-local-worker>.

Run local Codex CLI workers (one by default, up to four in the shared bounded pool) against a
**pinned local model** on loopback, in throwaway `git worktree`s, and get back evidence: a
patch, full-host test results, and an audit of every file that changed. Stdlib Python 3 only —
no install step, no dependencies.

Neither the launcher nor a worker ever merges into, commits to, or resets the primary checkout.
Committing, pushing and cleaning up accepted work belongs to the reviewing parent under the
user-wide completion policy (see [Rollback](#rollback)); the launcher itself only produces
evidence. Out-of-scope edits are detected after the run and reported as violations, even when
the tests pass.

**Default installation: the native plugin plus its `setup` skill** (see
[Installation](#installation)). Everything below the plugin path is the legacy manual route.

## Layout

- `bin/local-worker` — the only entry point (a `sh` shim over `src/local_worker.py`).
- `src/local_worker.py` — contract validation, preflight, worktree setup, worker launch,
  full-host verification, post-run audit, report writing.
- `src/worker_pool.py` — bounded shared-state concurrency: the slot lease pool, the
  owned-path overlap check and the per-simulator resource lease. No git, no subprocess.
- `src/native_readiness.py` — versioned native plan schema and the positive
  build/install/launch/XCTest gate. Native jobs run on the same full-host backend as every
  other job.
- `src/apple_verification.py` — simulator validation and the **legacy** confined
  `apple-seatbelt` helpers (its deny-default profile, `sandbox-exec` wrapper and confined
  readiness probe), which are selectable only programmatically and are unused by default.
- `scripts/prepare_pilot.py` — builds a throwaway git repo + task contract for a real pilot.
- `examples/task.json` — task contract template.
- `skills/local-worker/SKILL.md` — skill that tells the window model to plan and review
  while the local model implements.
- `tests/` — the suite: contract/policy, git audit, fingerprint, process control, job
  pipeline, pool concurrency. Inference is always mocked; every git call is real.

## Requirements

- macOS (the only platform tested) with `python3` (stdlib only) and `git`. Linux is unverified.
- The Codex CLI on `PATH` (or `--codex-bin` / `LOCAL_WORKER_CODEX_BIN`).
- oMLX serving OpenAI-compatible endpoints on `http://127.0.0.1:8000/v1` **only**; any other
  endpoint is rejected before a request is made.
- `~/.omlx/settings.json` containing `auth.api_key`, `server.host`/`server.port` matching
  loopback `8000`, and `model.model_fallback: false`.
- `~/.omlx/model_settings.json` mapping the canonical model to its API alias. The defaults are
  `Qwen3.8-Flash-Next-UD-Q4_K_XL` and `qwen-3.8-nf-splash`; another model is selected with
  `--canonical-model`/`--api-model` (see [Choosing the model](#choosing-the-model)). A model
  with no alias in oMLX is used by passing its own id as both values.

## Installation

### Native plugin (CLI or desktop)

This repository is itself the plugin package: `plugin.json` declares the `codex-local-worker` plugin, and
`.agents/plugins/marketplace.json` declares a private `codex-local-worker` marketplace whose single
`codex-local-worker` entry has `source: {"source": "local", "path": "./"}` — the path is relative to this
root, so the marketplace resolves the complete launcher (`bin/local-worker` plus `src/`) in
place. No MCP server and no hooks ship in this package.

```sh
PACKAGE=/absolute/path/outside/this/checkout/codex-local-worker   # a new path that does not exist yet
mkdir -p "$PACKAGE"
git -C /absolute/path/to/codex-local-worker archive HEAD | tar -x -C "$PACKAGE"
codex plugin marketplace add "$PACKAGE"
codex plugin add codex-local-worker@codex-local-worker
```

Export a clean tracked copy first and install that. Pointing `marketplace add` straight at a
source checkout also copies `.git/` plus whatever else the working tree happens to hold, so a
checkout under `~/.local-implement/worktrees/` carries stale worktree metadata into the installed
plugin; `git archive HEAD` yields only the committed tracked files of `HEAD`, so the package is
stable and that metadata cannot ride along. Re-export and re-add after each commit you want
installed. Read the installed root from `codex plugin add --json` instead of assuming the export
path is it, then reload Codex and run the `setup` skill, or its command directly, with `<root>`
set to that installed root:

```sh
python3 <root>/src/plugin_setup.py --check    # read-only report first
python3 <root>/src/plugin_setup.py --apply    # authorized installation
```

`$CODEX_HOME` is respected (default `~/.codex`). `--apply` is idempotent and owns only one small
managed block inside `$CODEX_HOME/AGENTS.md`, which points at the canonical policy shipped with the
plugin at `skills/local-worker/references/global-policy.md`. The authoritative policy text stays
in the package; only that tiny reference block is global, unrelated instructions in `AGENTS.md` are
preserved around it, and an existing file is backed up first. That block is routing, not
enforcement: it sends the planner selected in the Codex window to the bundled policy, and nothing
in this package enforces a model, a route, or a worker app-wide. The pre-rename hand-copied skill
`~/.codex/skills/local-implement/SKILL.md` — the old name belongs to that historical path — can be
retired after the plugin install is qualified; the current manual target is the
[manual launcher path](#manual-launcher-path-legacy) below.
The package is portable: no model weights, no credentials, and no cache or run state inside it —
state still lives outside the repository under `$LOCAL_WORKER_STATE_DIR`.

Both modes print one JSON object. `--check` reports plugin identity/version/root, the resolved
`$CODEX_HOME` and `$LOCAL_WORKER_STATE_DIR`, presence of the launcher and canonical policy,
`managed_block.state` (`absent`, `current`, `drift`, `malformed`), and the environment (Python, git,
Codex CLI, oMLX file presence **without** reading credentials, optional native tooling). `--apply`
adds a receipt — `changed`, `reason` (`installed`/`updated`/`migrated_legacy`/noop), `destination`,
`backup`, and the hashes of what is installed — after copying the previous bytes to
`$LOCAL_WORKER_STATE_DIR/setup-backups/` and replacing atomically; a second apply writes nothing
and makes no extra backup. Exit codes: `0` ok, `2` usage, `3` malformed package, `4` malformed
managed block, `5` failed apply, `6` an explicitly requested `--preflight` that failed, and `7` a
nonempty or unreadable `$CODEX_HOME/AGENTS.override.md` — Codex reads that override before
`AGENTS.md`, so while it exists the managed block is not an active default: `--check` exits `7`
with the blocker and `--apply` refuses before any backup or write, and the override's contents are
never printed. `--preflight` is delegated to the packaged `bin/local-worker preflight` and
`endpoint_passed` is true only when that exits `0` **and** prints a valid launcher success receipt:
a JSON object with nonempty `endpoint`, `canonical_model` and `api_model`, `model_fallback`
exactly `false`, and a `served_models` list containing `api_model`. Any other output — arbitrary
JSON such as `[]`, a blocked report, or exit `0` with no JSON — fails with the reason naming what
was missing, so a receipt shape is never mistaken for an endpoint claim. Migration is narrow: a
global `AGENTS.md` that matches the historical hand-written policy byte-for-byte is reduced to a
short heading plus the managed block, and anything else is kept as it is.

### Migration and compatibility

Canonical environment names are `$LOCAL_WORKER_STATE_DIR` and `$LOCAL_WORKER_CODEX_BIN`. The
pre-rename `$LOCAL_IMPLEMENT_STATE_DIR` and `$LOCAL_IMPLEMENT_CODEX_BIN` work **only as
fallbacks**: they are read when the canonical name is unset or empty, and equal values in both
names are the supported compatibility case. Two *different* nonempty values for one pair refuse
(usage error, before any backup or write) instead of guessing, because silently picking one would
split one shared pool or run a Codex binary the operator did not choose. An explicit CLI setting
(`--state-dir`, `--codex-bin`) is the way to resolve such a conflict deliberately: it wins over
both environment names.

The default state root `~/.local-implement` is kept **deliberately**. That directory is the one
existing shared pool — kernel locks, slots, leases, report history and preserved worktrees — so a
`~/.local-worker` root would split the pool and orphan unmerged evidence. It is a retained
compatibility path, not a missed rename; pass `--state-dir` or set `$LOCAL_WORKER_STATE_DIR` to
start a separate root.

The retired 0.1.0 package name `local-qwen` is **migration input only**: nothing installs or
routes to it any more. The current package is `codex-local-worker`; the launcher and skill inside
it are `local-worker`. Such a pointer is reported by `--check` as `managed_block.state`
`legacy_pointer`; `--apply` replaces the old `local-qwen` marker pair in place with exactly one
`local-worker` block, backs the previous bytes up under `$LOCAL_WORKER_STATE_DIR/setup-backups/`,
preserves every byte outside the block, and reports `reason` `migrated_legacy_pointer`; a second
apply writes nothing. Mixed, duplicated or unbalanced old/new markers are malformed: setup refuses
with the managed-block exit code, no backup and no write, because two competing routing policies
must never be left behind.

### Manual launcher path (legacy)

There is nothing to install. Make the launcher reachable and use it:

```sh
chmod +x bin/local-worker
/absolute/path/to/codex-local-worker/bin/local-worker preflight
```

Optional, per-user, reversible symlink instead of copying anything:

```sh
ln -s /absolute/path/to/codex-local-worker/bin/local-worker ~/.local/bin/local-worker
```

**Legacy — prefer the plugin install above.** If the plugin cannot be installed, copy the skill to
`~/.codex/skills/local-worker/SKILL.md` **together with the entire
`skills/local-worker/references/` folder** (`global-policy.md`, `operating-policy.md`,
`apple-verification.md`): the skill links into those files, so a lone `SKILL.md` is a broken
install. This path needs **no global binary** — the launcher is still invoked by its absolute
repository path above, never installed to `PATH`. A new chat or reload may be needed before Codex
picks the skill up, and even then it is advisory — it enforces nothing app-wide, it adds no managed
block in `$CODEX_HOME/AGENTS.md`, and it migrates nothing.

All state lives outside the repository, under `$LOCAL_WORKER_STATE_DIR`
(default `~/.local-implement`): `worker.lock`, `coordinator.lock`, `slots/slot-N.lock`,
`leases/slot-N.json`, `resources/apple-simulator-<uuid>.lock`, `reports/<job-id>/`,
`worktrees/<task-id>/`. One state directory is the whole pool — separate roots share no
locks.

## Rollback

Nothing writes into your repository. To stop using the skill, disable or remove **only** the
installed skill and leave all state in place, so unmerged work and evidence are never
destroyed:

```sh
rm ~/.codex/skills/local-worker/SKILL.md   # or delete just the local-worker skill folder
rm ~/.local/bin/local-worker               # if you created the optional symlink
```

For a plugin install, roll back only the plugin and its block: remove the plugin in Codex, run
`python3 <root>/src/plugin_setup.py --check` to see the block's `destination`, then delete the bytes
between `<!-- local-worker begin -->` and `<!-- local-worker end -->` in `$CODEX_HOME/AGENTS.md` and
leave the rest of that file alone. Every superseded version is in
`$LOCAL_WORKER_STATE_DIR/setup-backups/`.

Do **not** `rm -rf ~/.local-implement`: that state root holds `reports/`, logs, and the
preserved `worktrees/`, so deleting it would destroy unmerged work and the evidence you
review before applying anything.

Your repository is untouched by design. If a job reported `ok` and you already applied
`changes.patch` yourself, revert it with your own git workflow. The launcher never runs
`git commit`, `git merge`, `git reset`, or `git checkout` against the primary checkout, and
never deletes a worktree, so evidence survives a run.

Completion cleanup is a **mandatory step for the reviewing parent on every task**; what stays
non-automatic is who performs it. Neither a worker nor the launcher ever commits, pushes or
removes a worktree on its own, so evidence survives a run and the parent takes the checkpoint:
inventory `git worktree list` and `<state>/worktrees/`, commit and push accepted task changes to
the task branch, keep the reports and patches of active jobs, then archive and remove every
completed temporary or linked worktree, **dirty drafts included once their archive verified**,
without asking again.

Make the work recoverable first: a ref plus a `git bundle verify`-checked bundle, and archives
of any native artifacts, written outside the tree being removed. The archive helper verifies the
copy before it runs `git worktree remove`, so a verified archive covers an uncommitted draft the
same way it covers a clean tree; without that verification, keep the worktree. Never remove the
primary checkout, an active or claimed worktree, or the only copy of unarchived work.

Reuse the current checkout for routine planner work instead of adding planner or app worktrees;
the launcher's per-job worktrees remain required during execution, because they are what isolates
and evidences worker scope, and they too are archived and removed at completion once the parent
has reviewed the work. Keep primary and unrelated active work intact, and report any push or
safe-cleanup blocker explicitly instead of claiming completion.

## Usage

Preflight checks pinned model routing only; no worker starts and the API key is never printed:

```sh
bin/local-worker preflight
bin/local-worker preflight --settings ~/.omlx/settings.json \
  --model-settings ~/.omlx/model_settings.json
```

Run one job:

```sh
bin/local-worker run --task-file /absolute/path/task.json
bin/local-worker run --task-file /absolute/path/task.json --json
bin/local-worker run --task-file task.json --state-dir /tmp/state --codex-bin codex
bin/local-worker run --task-file task.json --workstream feature-x --job-kind implementation
bin/local-worker run --task-file task.json --workstream feature-x --job-kind repair
bin/local-worker status --state-dir "$LOCAL_WORKER_STATE_DIR" --job-id <job-id>
bin/local-worker run --task-file task.json --lock-timeout 120
bin/local-worker run --task-file task.json --max-workers 4   # reviewed parallel ceiling
```

For native Apple work, also pass `--apple-simulator-udid <UUID>` and
`--native-preflight-plan /absolute/path/plan.json`. Use `--preflight-only` to build, install,
launch, and positively test the app on that same selected simulator before starting a worker.
The versioned plan format and required evidence are in
[`skills/local-worker/references/apple-verification.md`](skills/local-worker/references/apple-verification.md).

| Flag | Meaning |
| --- | --- |
| `--task-file` | JSON task contract (required for `run`) |
| `--state-dir` | reports/logs/worktrees root; default `$LOCAL_WORKER_STATE_DIR` or `~/.local-implement` |
| `--codex-bin` | Codex CLI path; default `$PATH` or `$LOCAL_WORKER_CODEX_BIN` |
| `--lock-timeout` | seconds to wait for the worker lock (default 30); waits count against the job budget |
| `--max-workers` | ceiling for the **shared** state directory: `1` (default) keeps the exclusive `worker.lock`; `2`-`4` also takes one exclusive slot lease. Integer 1-4 only, validated before any worktree is created; the launcher never spawns a worker by itself |
| `--no-verify` | skip verification commands; they are simply **not run**, so they cannot satisfy acceptance |
| `--apple-simulator-udid` | opt in (default: unset): scope native work to one tvOS simulator UUID under an exclusive resource lease. Verification stays on the full host with the same model pins; the native gate must positively build, install, launch and run XCTest on that device |
| `--native-preflight-plan` | versioned JSON plan for fresh build, install, launch, and positive XCTest on the selected simulator, before the worker starts |
| `--preflight-only` | run model and native gates and save their evidence without starting a worker |
| `--workstream` | stable repair-history key; defaults to `default`, and changing task/job IDs does not reset it |
| `--job-kind` | `implementation` for initial work or `repair` for a follow-up |
| `status` | read live or completed progress and measured usage for a state directory/job |
| `--settings`, `--model-settings` | oMLX settings paths |
| `--canonical-model` | oMLX model id to pin (`run`, `preflight`); env `LOCAL_WORKER_CANONICAL_MODEL`, default `Qwen3.8-Flash-Next-UD-Q4_K_XL` |
| `--api-model` | id oMLX serves that model under; the model's own id when it has no alias (`run`, `preflight`); env `LOCAL_WORKER_API_MODEL`, default `qwen-3.8-nf-splash` |
| `--context-window` | `model_context_window` pinned for the worker, 4096-1048576 (`run`, `preflight`); env `LOCAL_WORKER_CONTEXT_WINDOW`, default `65536` |
| `--attempt-timeout` | per-attempt worker cap in seconds, 5-3600, still bounded by the job budget (`run`); env `LOCAL_WORKER_ATTEMPT_SECONDS`, default `300` |
| `--json` | print the full report instead of the summary |

### Choosing the model

The model settings resolve as CLI flag, then environment variable, then default. Whatever is
chosen is pinned and self-checked exactly like the defaults: the worker argv must carry that
alias and context window, and any other value is refused as drift. Example for a model that oMLX
serves under its own id, on a server capped at 32K tokens:

```sh
export LOCAL_WORKER_CANONICAL_MODEL=Qwen3.5-9B-OptiQ-4bit
export LOCAL_WORKER_API_MODEL=Qwen3.5-9B-OptiQ-4bit
export LOCAL_WORKER_CONTEXT_WINDOW=32768
export LOCAL_WORKER_ATTEMPT_SECONDS=600
bin/local-worker preflight
```

An alias is never guessed: when oMLX has an alias for the model, `--api-model` must be that
alias, and the bare id is accepted only when the model has none.

Exit codes: `0 ok`, `1 contract_invalid`, `2 preflight_failed`, `3 blocked`,
`4 worker_failed`, `5 audit_violation`, `6 tests_failed`, `7 verification_blocked`,
`8 timeout`, `9 model_mismatch`, `10 no_changes`. Non-zero is never a success.

`no_changes` (exit `10`) is the explicit no-op outcome: the worker ran cleanly and baseline
verification passed, but no worker-authored change survived to the final audit, so there is no
implementation to integrate. Two different runs share this status: a job where no attempt
produced an in-scope patch at all, and a job where an attempt did patch and that patch did not
survive — verification reverted it, or rewrote the same file so the bytes can no longer be
attributed to the worker. `changed_paths` and `changes.patch` may still be non-empty and retain
an artifact written by a verification command; the reviewed parent decides what, if anything, is
worth keeping. Passing baseline verification only means the contract's own commands passed: it is
not evidence that nothing else regressed, and it never proves an implementation.
The report says `worker_patch_present: false`. That flag is a provenance claim, not a diff
claim: before every worker attempt the launcher records the state of each path that already
differs from the base commit (content digest plus `lstat` mode, with an explicit marker for a
deletion), and after the worker it counts a path as authored by this attempt only when that
state changed, so a path that was clean before the attempt counts as new work. Those
path-to-state fingerprints accumulate across attempts and are pruned as soon as a path is no
longer changed from base or no longer holds the state the worker left it in. After
verification and the final audit the flag is true only while at least one retained
worker-authored path still exists in the final changed set in that same state, and a final
patch of zero bytes is never a patch. A file written only by a verification command, a worker
edit that verification reverted, and a leftover artifact from an earlier attempt therefore all
end `no_changes` even when `changed_paths` and `changes.patch` are non-empty. When
verification rewrites a file the worker also wrote, the launcher cannot prove which bytes are
the worker's, so it conservatively reports `no_changes` and requires parent review. The
fingerprints stay internal and are removed before the report is written. Every earlier failure
verdict keeps priority, and `no_changes` is not `worker_failed`: it is a normal terminal status
the archive helper accepts.

### Finishing a job: integrate, push, then archive

A worker never commits, merges, pushes or deletes anything: the reviewed parent owns
integration, and a linked worktree is disposable only once every byte of it is recoverable
somewhere else. Complete a job in this order:

1. Review `report.json`, `changes.patch`, the audit and every acceptance criterion, and
   integrate the reviewed commits on the task branch. Never apply an unreviewed patch to the
   primary checkout.
2. Resolve conflicts or gaps with a narrowly scoped repair job on the same workstream
   (`--job-kind repair`) rather than by hand-editing an unreviewed worktree.
3. Commit and push the task branch, then verify the remote really has it:
   `git log --oneline origin/<branch>..HEAD` must be empty.
4. Archive the completed, failed or superseded worktrees:

```sh
<root>/bin/local-worker-archive archive --repo /absolute/path/repo \
  --state-dir "$LOCAL_WORKER_STATE_DIR" --job-id <job-id>
```

The helper takes the exclusive `worker.lock`, refuses anything it does not own, and preserves
committed, dirty, untracked, ignored **and staged-index** content on a recovery ref
(`refs/local-worker/archive/<job-id>`) plus a verified `git bundle` and a hash-verified copy
under `<state-dir>/archives/<job-id>/` before it runs `git worktree remove` for that one path.
Its receipt is written as `prepared` before removal and only becomes `archived` afterwards, so
an interrupted run leaves verifiable evidence and refuses instead of archiving twice. Nothing
is force-removed before verification, and an existing ref, bundle or archive is never
overwritten. Where a failure leaves you depends on when it happened: a failure **before**
`git worktree remove` keeps every artifact and the worktree itself, while a failure **after**
the removal (for example a receipt write that could not complete) cannot restore that worktree
— the prepared receipt, recovery ref, bundle and archive copy are retained, the receipt stays
`prepared`, and the job requires reconciliation (verify the ref/bundle, replay the archive)
rather than being reported as archived. Exit codes: `0` archived (or an idempotent replay of a
verified receipt), `2` refused, `3` blocked by a live job or lease, `4` failed.

Never discard unrelated active work: the helper removes exactly the one worktree named by that
job's terminal report, and every other worktree, branch, stash or state directory is left
untouched. A finished task leaves no linked task worktree behind — the recovery refs, bundles
and archive copies stay.

### Task contract

Exactly these nine fields; unknown keys are refused, not ignored:

| Field | Rule |
| --- | --- |
| `task_id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, no path separators |
| `repo_path` | absolute, must exist |
| `base_ref` | git ref that resolves to a commit; no leading `-`, no `..`, `~`, `^`, `?`, `*`, `[`, `\` |
| `goal` | what to implement |
| `acceptance_criteria` | non-empty list of strings |
| `allowed_paths` | repo-relative; a directory entry matches its whole subtree |
| `test_commands` | argv arrays, run from the worktree root — never shell strings |
| `timeout_seconds` | 30–3600, one total budget for preflight + probe + attempts + all verification |
| `max_attempts` | 1 or 2; attempt 2 gets structured feedback from attempt 1 |

### Per-task MCP tools (`--tool-config`)

Task tooling is a separate schema-1 manifest, passed per job; nothing is inherited from the
plugin, the app configuration, or another task:

```sh
bin/local-worker run --task-file /absolute/path/task.json \
  --tool-config /absolute/path/tools.json
```

[`examples/tool-config.json`](examples/tool-config.json) is the working manifest for this
repository's own stdio fixture (`tests/fixtures/mcp_echo.py`, one `echo` tool). Every entry is
explicit: `required_commands`, the server `command` plus `args`, `env_vars`, and a non-empty
`enabled_tools` allowlist. Unknown keys, a missing allowlist, or both transports at once are
refused. A server that cannot be probed, a `required_commands` entry that is not executable,
and a named credential that is unset all **block the job before the worker starts**.

- Credentials are named, never valued: the manifest carries only environment variable *names*,
  and the selected value must be at least 4 characters so it can be redacted. A value never
  reaches argv, the prompt, the report, or the logs, and a literal `env`/`headers`/token value
  in the manifest is refused.
- Evidence keeps three states apart: `configured` (the manifest named it), `probed` (the
  metadata probe initialized the server and listed its tools), and `observed` (an explicit
  `mcp_tool_call` lifecycle event in that job's own stdout named that server and tool). A
  successful tool listing is not an observed call, and a tool name merely quoted inside a
  message, argument or output is not one either.
- The probe is a capability check only. It starts no model turn and is not the live worker
  backend; it cannot show that the worker can call a tool.
- HTTP servers are supported because Codex supports them, and they are validated the same way.
  Live qualification here was done against the **stdio** fixture only; no third-party HTTP
  service is qualified by this repository.

### Pilot

`scripts/prepare_pilot.py` creates a genuine, failing-test fixture with a deliberately
dirtied out-of-scope file, so a pilot exercises the audit as well as the worker. It refuses
an existing or relative `--root`, and performs no inference:

```sh
python3 scripts/prepare_pilot.py --root /tmp/pilot-1
bin/local-worker run --task-file /tmp/pilot-1/task.json --state-dir /tmp/pilot-1/state --json
```

It creates `/tmp/pilot-1/repo` (committed `add.py` returning `a - b`, `test_add.py` expecting
`add(2, 3) == 5`, `.gitignore` for `__pycache__`, `sentinel.txt`), commits it with a local
identity, then dirties `sentinel.txt` **after** the commit. `task.json` allows only `add.py`,
so a correct run fixes `add.py` and must report the pre-existing dirty `sentinel.txt` as an
unrelated dirty primary file rather than as a violation caused by the worker.

### Apple simulator verification

Apple jobs use a versioned native plan to build a fresh app, install and launch it, then run a
positive XCTest on the selected device before any model attempt. Both the gate and the worker
run on the full host with the real toolchain, scoped to the one selected UUID; scope is not
confinement. The plan and evidence format are documented in
[Apple verification](skills/local-worker/references/apple-verification.md), with cache
preparation, native startup notes and trust limits in
[operating policy](skills/local-worker/references/operating-policy.md). Simulator
availability metadata is not qualification, and a command that could not run is reported as
blocked rather than passed.

## Local model alias

The launcher never loads your interactive `~/.codex/config.toml`; it pins everything with
explicit `-c` overrides and then **self-checks its own argv** before the first subprocess.
Any drift is a refusal (`blocked`), not a degraded run:

- `model=<api model>` (default `qwen-3.8-nf-splash`), `model_provider=local-omlx` with
  `base_url="http://127.0.0.1:8000/v1"`, `env_key=LOCAL_OMLX_API_KEY`, `wire_api=responses`,
  `requires_openai_auth=false`, and **zero** request/stream retries (a retry could be served
  by something else). Zero retries exist so a failure stays **bounded and visible** — one
  attempt, one honest status — rather than being silently retried onto a different backend.
- `approval_policy=never`, `web_search=disabled`, `sandbox_mode=danger-full-access`,
  `model_context_window=<context window>` (default `65536`), and `shell_environment_policy` restricted with
  `LOCAL_OMLX_API_KEY` excluded from the shell.
- Worker argv also carries `--ignore-user-config --ignore-rules -C <worktree>
  -s danger-full-access --json -o <last-message> -m <alias>`. `--ignore-rules` means the
  worker's execpolicy rules are not consulted; nothing prompts for approval.
- Verification runs **directly on the host** with the contract's argv array, unchanged and
  unwrapped, with network enabled; the report records `sandboxed: false` plus the
  `full-host-access` backend and `none` confinement. A command that could not run at all is
  reported `verification_blocked` with the concrete reason, never as a pass.
- Model identity rests on **routing evidence**, not a double check. The request config (the
  `-c` pins plus the argv self-check) and preflight (the alias must be served in `/v1/models`,
  with fallback disabled) are what route the job to the local model. The worker's own event
  stream is parsed for the routed model **when it includes one** — a conflicting value is
  `model_mismatch`, but the stream can legitimately omit the field, which is recorded as
  `absent`; that missing-model-metadata warning is not a model fallback and never a silent
  switch. For a live pilot, confirm routing in the oMLX server logs.

Environment isolation: proxy variables are stripped, cloud credential variables
(`OPENAI_*`, `ANTHROPIC_*`, `AWS_*`, …) are removed, the child gets a minimal base
environment plus the loopback key only, and the key is redacted from all report text.

### Worker JSONL output versus the app-server protocol

These are two different interfaces and the launcher uses only the first:

- **Worker output (`codex exec --json`)** is a one-way JSONL stream: one event per line, e.g.
  `thread.started`, `turn.started`/`turn.completed`/`turn.failed`, `item.started`/
  `item.completed`, `token_count`, `response.failed` and `error`. Nothing is written back to the
  worker; the launcher reads evidence only from the documented location inside each event —
  `thread_id` on `thread.started`, `model` at the top level or under `response`, the tool in the
  explicit `item` of a lifecycle event. A model name, item type or event type that tool output,
  file content or a message merely *quotes* is payload data, never routing, tool or failure
  evidence. `item.started` and `item.completed` are counted separately and deduplicated by item
  id; a documented terminal event makes the run `worker_failed` even when the exit code is `0`.
- **The app-server protocol** is a separate JSON-RPC session (`initialize`, `thread/start`,
  `turn/start`, `turn/steer`, server-initiated notifications) that can drive and steer a live
  turn. The launcher never speaks it: there is no mid-turn steer, no approval round-trip and no
  reconnection, which is exactly why `max_attempts` restarts a fresh `codex exec` process
  instead of steering one, and why an interrupted stream is reported from the persisted JSONL
  rather than resumed.

## Trust limits, audit, secrets

- **Trusted repository only.** Launcher git commands disable inherited hooks
  (`core.hooksPath=/dev/null`), fsmonitor, interactive editors, and external diff/textconv,
  and use argument arrays with no shell. Beyond that the repository is trusted input:
  custom clean/smudge filters or unusual git config in a cloned repo can still influence
  git behaviour.
- **The audit detects, it does not prevent.** It compares the worktree against the original
  base commit in a throwaway index, so committed, staged, unstaged, deleted, renamed (both
  sides) and untracked work is all visible. A worker that moves HEAD is reported as a policy
  violation and its commits are preserved for review, never reset or hidden.
- **The primary checkout is fingerprinted before and after**, by name *and* by content hash
  of every already-dirty path plus the HEAD and index digests, so a file that was dirty
  before the job cannot be silently edited again during it.
- **Full-host execution is not a boundary or read secrecy.** The worker runs with this
  process's own filesystem and network permissions, so anything readable in the worktree —
  and, beyond the repository, any file this host can read — must be treated as disclosed to
  the model;
  secrecy is not guaranteed for host-readable files outside the repo either. Do not put
  secrets, `.env` files, or credential stores in a repository you hand to this launcher;
  clone a scrubbed copy if you must.
- **Reports are patch evidence, not review.** The window model must read the diff itself;
  the tests that matter are the launcher's full-host verification, so re-run the launcher
  with a tighter contract rather than running checks against the primary checkout.
- **One worker at a time per state directory by default** (`fcntl.flock` on `worker.lock`);
  a second job is reported `blocked` rather than started. `--max-workers` 2-4 widens that
  one pool: the shared `worker.lock` plus one of at most 4 exclusive slot leases, so a
  default job and a parallel job still block each other in both directions. Same-repository
  contracts with overlapping owned paths are refused naming the other job (separate
  worktrees do not make overlapping edits mergeable), different repositories are
  independent, and `--apple-simulator-udid` takes an exclusive per-device lease so one
  simulator is never used twice at once. Kernel flock state is authoritative — a leftover
  `leases/*.json` is not an active job, and a killed holder releases everything.
  Concurrency is not throughput: every worker shares the same local oMLX.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

`tests/test_contract_policy.py` covers contract validation, loopback/alias pinning,
environment isolation, argv self-check, and full-host verification readiness (a readiness
command that runs yields `passed`; one that cannot run yields `blocked`). `tests/test_audit.py`
covers the git audit on real disposable repos (in-scope edits, staged, untracked, renames,
worker commits, ignored output, symlink escapes, scratch-dir placement) and the
primary-checkout fingerprint. `tests/test_run_job.py` covers job orchestration with a mocked
worker/model list (success and every failure path, preflight report and persistence, report
dir before preflight, budget exhaustion, second-attempt feedback, key never reaching the
shell env), plus lock exclusivity and process-group termination including children that
ignore `SIGTERM`.

`tests/test_worker_pool.py` covers the bounded pool with **real processes and real file
locks** (still no model, server, or simulator): 4 simultaneous leases and a 5th refused,
default-1 and parallel jobs mutually exclusive in both directions, same-repository owned-path
overlap refused while disjoint paths and other repositories are allowed, one simulator UUID
serialized case-insensitively while another device proceeds, a `SIGKILL`ed holder releasing
the kernel locks, no leaked slot after an exception or a timeout, and an invalid
`--max-workers` refused before any worktree exists.

`tests/test_apple_verification.py` covers the Apple backend with a faked toolchain: the
fixed profile's write bounds (the allowed roots plus the single literal `/dev/null` device node
and nothing else, the deny-then-allow ordering, and a refused widening of that exception to any
other path), `--apple-simulator-udid` validation, the `sandbox-exec` argv wrapper and its refusal
to wrap anything else, and the readiness probe against real-shaped `simctl` JSON — the
selected device resolved by simctl's own `isAvailable` flag, stdout parsed apart from stderr
diagnostics, the bounded scratch write that must pass, and the out-of-bounds write and loopback
connect that must be refused. A failing probe must produce a `blocked` report with concrete
per-check reasons.
One Darwin-only test validates the generated profile's **syntax** for real:
`sandbox-exec -p <profile> /usr/bin/true`. Exit `0` (suite running outside an outer sandbox) or
exit `71` with `sandbox_apply: Operation not permitted` (this process is already inside a
Seatbelt sandbox, so the nested policy parsed and was accepted) is a pass; exit `65` — the code
an invalid reporting modifier produces — and any other failure fail the suite.

Nothing in the suite calls a model or a server: `run_bounded` is replaced with a fake for
job tests, and the model list is an injected callable. Real subprocesses are used only for
the process-control tests. **No test runs `xcodebuild`, `xcrun`, a simulator, or anything
that touches the real Apple toolchain**: the Apple toolchain checks are faked — the only real
Darwin-side call is the legacy fixed `sandbox-exec -p <profile> /usr/bin/true` syntax check
above, which runs nothing but `true` inside that historical profile — and the real toolchain is
exercised only by the launcher itself in a live run.
