# codex-local-first

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

- `bin/local-implement` — the only entry point (a `sh` shim over `src/local_worker.py`).
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
- `skills/local-implement/SKILL.md` — skill that tells the window model to plan and review
  while the local model implements.
- `tests/` — the suite: contract/policy, git audit, fingerprint, process control, job
  pipeline, pool concurrency. Inference is always mocked; every git call is real.

## Requirements

- macOS (the only platform tested) with `python3` (stdlib only) and `git`. Linux is unverified.
- The Codex CLI on `PATH` (or `--codex-bin` / `LOCAL_IMPLEMENT_CODEX_BIN`).
- oMLX serving OpenAI-compatible endpoints on `http://127.0.0.1:8000/v1` **only**; any other
  endpoint is rejected before a request is made.
- `~/.omlx/settings.json` containing `auth.api_key`, `server.host`/`server.port` matching
  loopback `8000`, and `model.model_fallback: false`.
- `~/.omlx/model_settings.json` mapping the canonical model
  `Qwen3.8-Flash-Next-UD-Q4_K_XL` to the alias `qwen-3.8-nf-splash`.

## Installation

### Native plugin (CLI or desktop)

This repository is itself the plugin package: `plugin.json` declares the `local-qwen` plugin, and
`.agents/plugins/marketplace.json` declares a private `local-qwen-tools` marketplace whose single
`local-qwen` entry has `source: {"source": "local", "path": "./"}` — the path is relative to this
root, so the marketplace resolves the complete launcher (`bin/local-implement` plus `src/`) in
place. No MCP server and no hooks ship in this package.

```sh
PACKAGE=/absolute/path/outside/this/checkout/local-qwen   # a new path that does not exist yet
mkdir -p "$PACKAGE"
git -C /absolute/path/to/codex-local-first archive HEAD | tar -x -C "$PACKAGE"
codex plugin marketplace add "$PACKAGE"
codex plugin add local-qwen@local-qwen-tools
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
plugin at `skills/local-implement/references/global-policy.md`. The authoritative policy text stays
in the package; only that tiny reference block is global, unrelated instructions in `AGENTS.md` are
preserved around it, and an existing file is backed up first. That block is routing, not
enforcement: it sends the planner selected in the Codex window to the bundled policy, and nothing
in this package enforces a model, a route, or a worker app-wide. A legacy hand-copied
`~/.codex/skills/local-implement/SKILL.md` can be retired after the plugin install is qualified.
The package is portable: no model weights, no credentials, and no cache or run state inside it —
state still lives outside the repository under `$LOCAL_IMPLEMENT_STATE_DIR`.

Both modes print one JSON object. `--check` reports plugin identity/version/root, the resolved
`$CODEX_HOME` and `$LOCAL_IMPLEMENT_STATE_DIR`, presence of the launcher and canonical policy,
`managed_block.state` (`absent`, `current`, `drift`, `malformed`), and the environment (Python, git,
Codex CLI, oMLX file presence **without** reading credentials, optional native tooling). `--apply`
adds a receipt — `changed`, `reason` (`installed`/`updated`/`migrated_legacy`/noop), `destination`,
`backup`, and the hashes of what is installed — after copying the previous bytes to
`$LOCAL_IMPLEMENT_STATE_DIR/setup-backups/` and replacing atomically; a second apply writes nothing
and makes no extra backup. Exit codes: `0` ok, `2` usage, `3` malformed package, `4` malformed
managed block, `5` failed apply, `6` an explicitly requested `--preflight` that failed, and `7` a
nonempty or unreadable `$CODEX_HOME/AGENTS.override.md` — Codex reads that override before
`AGENTS.md`, so while it exists the managed block is not an active default: `--check` exits `7`
with the blocker and `--apply` refuses before any backup or write, and the override's contents are
never printed. `--preflight` is delegated to the packaged `bin/local-implement preflight` and
`endpoint_passed` is true only when that exits `0` **and** prints a valid launcher success receipt:
a JSON object with nonempty `endpoint`, `canonical_model` and `api_model`, `model_fallback`
exactly `false`, and a `served_models` list containing `api_model`. Any other output — arbitrary
JSON such as `[]`, a blocked report, or exit `0` with no JSON — fails with the reason naming what
was missing, so a receipt shape is never mistaken for an endpoint claim. Migration is narrow: a
global `AGENTS.md` that matches the historical hand-written policy byte-for-byte is reduced to a
short heading plus the managed block, and anything else is kept as it is.

### Manual launcher path (legacy)

There is nothing to install. Make the launcher reachable and use it:

```sh
chmod +x bin/local-implement
/absolute/path/to/codex-local-first/bin/local-implement preflight
```

Optional, per-user, reversible symlink instead of copying anything:

```sh
ln -s /absolute/path/to/codex-local-first/bin/local-implement ~/.local/bin/local-implement
```

**Legacy — prefer the plugin install above.** If the plugin cannot be installed, copy the skill to
`~/.codex/skills/local-implement/SKILL.md` **together with the entire
`skills/local-implement/references/` folder** (`global-policy.md`, `operating-policy.md`,
`apple-verification.md`): the skill links into those files, so a lone `SKILL.md` is a broken
install. This path needs **no global binary** — the launcher is still invoked by its absolute
repository path above, never installed to `PATH`. A new chat or reload may be needed before Codex
picks the skill up, and even then it is advisory — it enforces nothing app-wide, it adds no managed
block in `$CODEX_HOME/AGENTS.md`, and it migrates nothing.

All state lives outside the repository, under `$LOCAL_IMPLEMENT_STATE_DIR`
(default `~/.local-implement`): `worker.lock`, `coordinator.lock`, `slots/slot-N.lock`,
`leases/slot-N.json`, `resources/apple-simulator-<uuid>.lock`, `reports/<job-id>/`,
`worktrees/<task-id>/`. One state directory is the whole pool — separate roots share no
locks.

## Rollback

Nothing writes into your repository. To stop using the skill, disable or remove **only** the
installed skill and leave all state in place, so unmerged work and evidence are never
destroyed:

```sh
rm ~/.codex/skills/local-implement/SKILL.md   # or delete just the local-implement skill folder
rm ~/.local/bin/local-implement               # if you created the optional symlink
```

For a plugin install, roll back only the plugin and its block: remove the plugin in Codex, run
`python3 <root>/src/plugin_setup.py --check` to see the block's `destination`, then delete the bytes
between `<!-- local-qwen begin -->` and `<!-- local-qwen end -->` in `$CODEX_HOME/AGENTS.md` and
leave the rest of that file alone. Every superseded version is in
`$LOCAL_IMPLEMENT_STATE_DIR/setup-backups/`.

Do **not** `rm -rf ~/.local-implement`: that state root holds `reports/`, logs, and the
preserved `worktrees/`, so deleting it would destroy unmerged work and the evidence you
review before applying anything.

Your repository is untouched by design. If a job reported `ok` and you already applied
`changes.patch` yourself, revert it with your own git workflow. The launcher never runs
`git commit`, `git merge`, `git reset`, or `git checkout` against the primary checkout, and
never deletes a worktree, so evidence survives a run.

Pruning is a reviewed checkpoint, never automatic: inventory `git worktree list` and
`<state>/worktrees/`, keep the reports and patches of accepted or active jobs, and before
removing a superseded **clean** worktree make its detached commits recoverable (a ref plus
a `git bundle verify`-checked bundle, and archives of any native artifacts). Never remove
the primary checkout, an active or claimed worktree, or the only copy of unarchived work.

The user-wide completion policy applies to the reviewing parent: commit and push accepted
task changes to the task branch, preserve detached history and needed ignored artifacts,
then remove completed linked worktrees. Workers and the launcher still never perform these
Git/cleanup operations automatically. Keep primary/unrelated active work intact; report any
push or safe-cleanup blocker explicitly.

## Usage

Preflight checks pinned model routing only; no worker starts and the API key is never printed:

```sh
bin/local-implement preflight
bin/local-implement preflight --settings ~/.omlx/settings.json \
  --model-settings ~/.omlx/model_settings.json
```

Run one job:

```sh
bin/local-implement run --task-file /absolute/path/task.json
bin/local-implement run --task-file /absolute/path/task.json --json
bin/local-implement run --task-file task.json --state-dir /tmp/state --codex-bin codex
bin/local-implement run --task-file task.json --workstream feature-x --job-kind implementation
bin/local-implement run --task-file task.json --workstream feature-x --job-kind repair
bin/local-implement status --state-dir "$LOCAL_IMPLEMENT_STATE_DIR" --job-id <job-id>
bin/local-implement run --task-file task.json --lock-timeout 120
bin/local-implement run --task-file task.json --max-workers 4   # reviewed parallel ceiling
```

For native Apple work, also pass `--apple-simulator-udid <UUID>` and
`--native-preflight-plan /absolute/path/plan.json`. Use `--preflight-only` to build, install,
launch, and positively test the app on that same selected simulator before starting a worker.
The versioned plan format and required evidence are in
[`skills/local-implement/references/apple-verification.md`](skills/local-implement/references/apple-verification.md).

| Flag | Meaning |
| --- | --- |
| `--task-file` | JSON task contract (required for `run`) |
| `--state-dir` | reports/logs/worktrees root; default `$LOCAL_IMPLEMENT_STATE_DIR` or `~/.local-implement` |
| `--codex-bin` | Codex CLI path; default `$PATH` or `$LOCAL_IMPLEMENT_CODEX_BIN` |
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
| `--json` | print the full report instead of the summary |

Exit codes: `0 ok`, `1 contract_invalid`, `2 preflight_failed`, `3 blocked`,
`4 worker_failed`, `5 audit_violation`, `6 tests_failed`, `7 verification_blocked`,
`8 timeout`, `9 model_mismatch`. Non-zero is never a success.

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

### Pilot

`scripts/prepare_pilot.py` creates a genuine, failing-test fixture with a deliberately
dirtied out-of-scope file, so a pilot exercises the audit as well as the worker. It refuses
an existing or relative `--root`, and performs no inference:

```sh
python3 scripts/prepare_pilot.py --root /tmp/pilot-1
bin/local-implement run --task-file /tmp/pilot-1/task.json --state-dir /tmp/pilot-1/state --json
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
[Apple verification](skills/local-implement/references/apple-verification.md), with cache
preparation, native startup notes and trust limits in
[operating policy](skills/local-implement/references/operating-policy.md). Simulator
availability metadata is not qualification, and a command that could not run is reported as
blocked rather than passed.

## Local model alias

The launcher never loads your interactive `~/.codex/config.toml`; it pins everything with
explicit `-c` overrides and then **self-checks its own argv** before the first subprocess.
Any drift is a refusal (`blocked`), not a degraded run:

- `model=qwen-3.8-nf-splash`, `model_provider=local-omlx` with
  `base_url="http://127.0.0.1:8000/v1"`, `env_key=LOCAL_OMLX_API_KEY`, `wire_api=responses`,
  `requires_openai_auth=false`, and **zero** request/stream retries (a retry could be served
  by something else). Zero retries exist so a failure stays **bounded and visible** — one
  attempt, one honest status — rather than being silently retried onto a different backend.
- `approval_policy=never`, `web_search=disabled`, `sandbox_mode=danger-full-access`,
  `model_context_window=65536`, and `shell_environment_policy` restricted with
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
