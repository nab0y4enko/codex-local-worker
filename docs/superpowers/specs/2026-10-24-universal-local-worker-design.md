# Universal Local Worker — design

Date: 2026-10-24. Status: decisions complete; approved for planning on 2026-10-24.

## 1. Intent

The cloud model selected in the Codex window (the **planner**) should spend as few tokens as
possible. It plans, splits work into small jobs, reviews, and integrates. Everything bulky —
reading code to answer questions and writing code — goes to a **local model served by oMLX**
on the same Mac, through this plugin's launcher.

Three things must hold, or the plugin has failed even if it "works":

1. **Every task finishes.** A failed local job is a decision point, never a dead end.
2. **Results are correct and checked.** The launcher, not the model, verifies scope, tests,
   native builds, and — for UI work — produces screenshots a human can look at.
3. **Everything is observable.** At the end the user gets a complete account: every job, every
   worker and model that ran, attempts, durations, tokens (local vs cloud), escalations, and
   suggested fixes to the plugin itself.

The plugin must run unchanged on any Mac with any model loaded in oMLX. The user has two Macs
with different models; nothing about the model may be hardcoded.

### The reference scenario

> Project Tiviary. The experimental branch has a new tvOS target with a Settings screen. Make it
> match the original app, but with UIKit and TVKit only; differences are allowed only when they
> follow Apple's native way. Plan it, implement everything with local workers, and give full
> statistics on agents, workers, and tokens at the end.

The planner sends local **read jobs** ("list every Settings control on the experimental branch:
type, order, frame source, custom vs native"; "summarize how the original app builds this
screen"), plans from those compact answers, sends local **write jobs** per screen section,
reviews each patch plus **before/after screenshots** from the tvOS simulator, escalates any job
the local model cannot do, and ends with a **session summary**.

## 2. Goals

- G1. Discover the server, the loaded model, and its limits from oMLX at the start of every job.
  No model id, alias, port, context size, or time limit is hardcoded.
- G2. Two job kinds: **read** (no file changes, structured answer with verified citations) and
  **write** (changes in a throwaway worktree, audited and verified).
- G3. A failure is classified as **environment** (setup, permission, limit) or **capability**
  (the model could not do it), and the launcher says what to do next.
- G4. Escalation for capability failures: local → a cloud model chosen by the planner from the
  live Codex catalog, run through the same launcher pipeline → the planner itself as last resort.
- G5. Environment failures produce a concrete remediation for the plugin or the machine, shown to
  the user, and are retried once automatically when the fix is safe to apply per job.
- G6. Native Apple verification works for any simulator platform the installed Xcode provides
  (iOS, tvOS, watchOS, visionOS) and for macOS apps; it can capture before/after screenshots.
- G7. A session ledger records every job; `summary` renders the final report including the
  planner's own token usage from its Codex session log.
- G8. Fix all 15 findings of the 2026-10-24 review (mapping in §12).
- G9. Follow Codex conventions where Codex already provides the mechanism (plugin layout,
  `--output-schema`, `--ephemeral`, sandbox modes, model catalog), instead of re-implementing it.

## 3. Non-goals

- Local servers other than oMLX (LM Studio, Ollama, vLLM, llama.cpp). The discovery layer is an
  interface so another server can be added later, but only oMLX is implemented and qualified.
- Platforms other than macOS.
- Dollar cost accounting. The Codex catalog carries no prices; the report counts tokens only.
- A UI beyond the conversation widget of §6.13. The CLI terminal UI gets no plugin UI (Codex does
  not offer one); there it is Markdown + JSON reports, PNG screenshots, and notifications.
- A ChatGPT Apps SDK app hosted by OpenAI (the `apps` / `.app.json` mechanism); everything stays
  local.
- Automatic merging into the user's branch. Integration stays the planner's reviewed action.

## 4. Anti-goals

- Silent fallback to the cloud. Every cloud use is an explicit, recorded escalation.
- A global instruction that changes the behaviour of every Codex session (the current
  `AGENTS.md` block that forbids the planner to implement and auto-commits/pushes).
- Reporting success without launcher-side evidence (tests run by the launcher, native gate,
  audit, verified citations).
- Tuning values chosen for one model on one Mac.

## 5. Evidence gathered (this Mac, 2026-10-24)

oMLX 0.7.3.dev1 (app bundle `/Applications/oMLX.app`, server `omlx-server` on 127.0.0.1:8000):

| Endpoint | Auth | What it gives |
| --- | --- | --- |
| `GET /health` | none | `status` (`healthy` / 503 `loading`), `default_model`, pool `model_count`, `loaded_count`, memory ceiling |
| `GET /api/status` | API key | `version`, `loaded_models[]`, `models_loading`, `active_requests`, `waiting_requests`, `avg_prefill_tps`, `avg_generation_tps`, memory used/max |
| `GET /v1/models` | inference key | every *discovered* model (loaded or not), alias-substituted id, `max_model_len` |
| `GET /v1/models/status` | API key | per model: `id`, `loaded`, `is_loading`, `pinned`, `engine_type` (`vlm`/…), `model_type`, `model_context_length`, `max_context_window` (effective), `max_tokens`, `model_alias`, `is_helper`, `thinking_default`, `reasoning_efforts` |
| `POST /v1/models/{id}/load` | API key | loads a model (blocking) |
| `POST /v1/responses` | inference key | Responses API with streaming; `response.model`, `usage.input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens_details.reasoning_tokens` |

- `/v1/models` lists models on disk, not only loaded ones; `/v1/models/status.loaded` is the
  truth for "loaded".
- Effective context = per-model override, else the model's native length (optionally clamped by
  `sampling.max_context_window_policy`), lowered to what a loaded engine planned for memory.
  `sampling.max_context_window` (32768 here) is **only a fallback default, not a cap**. Here the
  effective value is 262144. (The interim "context warning" added on 2026-10-24 compared against
  the wrong field and is superseded by this design.)
- Served model today: `Qwen3.5-9B-OptiQ-4bit`, `engine_type: vlm` (accepts images),
  `thinking_default: true` (a one-word reply spent 200 reasoning tokens, 8.7 s).
- A real pilot write job through the current launcher succeeded in 42 s: 47,610 input tokens
  (39,169 cached), 627 output, 5 tool calls. Codex logged `Model metadata for <model> not found.
  Defaulting to fallback metadata` and `unsupported call: apply_patch`; the model fell back to
  shell edits (BSD `sed -i` failed once). The worker event stream carried no `model` field.

Codex CLI 0.164.0:

- `codex exec` has `--ephemeral`, `--output-schema`, `--ignore-user-config`, `--ignore-rules`,
  `-s read-only|workspace-write|danger-full-access`, `--add-dir`.
- `-c model_catalog_json=<path>` is honoured: a generated entry for the local model changes the
  model's instructions and tool set. The right field values for a local model are not yet known
  (copying a cloud entry brings cloud "code mode" tools).
- `codex debug models` returns the cloud catalog: `slug`, `description` (e.g. `gpt-6-luna`: "Fast
  and affordable model for easier tasks"), `context_window`, reasoning levels, visibility. No
  prices.
- The planner has `spawn_agent` with `model` and `reasoning_effort` overrides.
- Codex processes see `CODEX_THREAD_ID`. Session rollouts (`$CODEX_HOME/sessions/**/rollout-*.jsonl`)
  contain `session_meta` (id, model provider, context window) and `token_count` events with
  cumulative `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`.
- The current root `plugin.json` installs and its skills are discovered; first-party plugins use
  `.codex-plugin/plugin.json`.

Worker permissions, probed with real commands from a linked worktree:

| Action | Codex `workspace-write` | Seatbelt `(allow default)` + denials |
| --- | --- | --- |
| write worktree, `$TMPDIR` | allowed | allowed |
| write `$HOME`, DerivedData, SwiftPM cache | denied | allowed |
| read `~/.codex/auth.json`, `~/.ssh` | allowed (denied only via profile) | denied (listed) |
| network, loopback to oMLX | denied | allowed |
| `xcrun simctl` | denied (even with a profile granting write `/` and network) | allowed |
| `git add` / commit in the worktree | denied | allowed |
| write primary checkout files, `.git/HEAD`, new branch | — | denied (listed) |

Codex permission profiles accept `filesystem = { "<path>" = "read" | "write" | "deny" }` and
`network.*` settings, but cannot extend `:danger-full-access`.

Xcode 27.1 with iOS 27.0 and tvOS 27.0 runtimes; `xcrun simctl io <udid> screenshot` available;
two iOS simulators were already booted by other sessions.

## 6. Architecture

Stdlib-only Python 3.9+ (the Xcode `python3`), one package under `src/local_worker/`, one CLI
entry point `bin/local-worker`.

```
planner (Codex window model)
   │  skill: local-worker  (decompose → jobs, interpret next_action, escalate, summarize)
   ▼
bin/local-worker  ── preflight | models | run | status | summary | archive | doctor | workstream
   │
   ├── discovery/omlx.py      server + loaded-model profile (live, per job)
   ├── contract.py            job contract v2 (read | write), validation
   ├── codex_cli.py           argv, generated model catalog, isolated CODEX_HOME, self-check,
   │                          cloud catalog
   ├── runner.py              process groups, streaming capture, stall watchdog, signals
   ├── events.py              the one parser of `codex exec --json` events
   ├── audit.py               worktree creation, scope audit, patch
   ├── verify/commands.py     contract test commands on the host
   ├── verify/native.py       simulator/macOS build-install-launch-test gate, screenshots
   ├── read_answers.py        output-schema validation + citation verification
   ├── classify.py            failure taxonomy → remediation + next_action
   ├── ledger.py              session ledger, planner rollout tokens, summary rendering
   ├── permissions.py         permissions list, Seatbelt wrapper profile (allow default + denials)
   ├── notify.py              macOS notifications (best effort)
   ├── pool.py                leases (existing)
   ├── archive.py             worktree archive/remove (existing, fixed)
   └── dashboard/             later phase: stdio MCP server + static MCP Apps widget (§6.13)
```

Each module has one job and a small interface; `run` composes them. The existing modules are
moved, not rewritten, where they already work (pool, audit core, archive core).

### 6.1 Discovery (G1)

`discover(settings_path) -> ServerProfile` runs at preflight and at the start of every job, and
before every attempt.

1. Read `~/.omlx/settings.json`: `server.host`, `server.port`, `auth.api_key`,
   `model.model_fallback`. Host must be loopback; the port is whatever oMLX uses.
2. `GET /health` — must be `healthy` (503 `loading` → environment failure `server_loading`).
3. `GET /v1/models/status` and `GET /api/status`.
4. Select the model:
   - candidates = `loaded: true`, not `is_helper`, `engine_type` in the chat-capable set
     (text and vision LLMs; embedding, rerank, STT, markitdown excluded);
   - if the job or caller names a model (`--model`), it must be a candidate (by id or alias);
   - else exactly one candidate → it; else oMLX `default_model` if it is a candidate;
   - else refuse with the candidate list (`model_ambiguous`). Zero candidates → `model_not_loaded`
     listing discovered-but-unloaded models; `--load <id>` explicitly loads one.
5. Profile: `api_model` (alias if set, else id), `canonical_id`, `context_window` =
   `max_context_window`, `max_output_tokens` = `max_tokens`, `accepts_images`
   (`engine_type == vlm`), `thinking_default`, `reasoning_efforts`, measured `prefill_tps` /
   `generation_tps` (from `/api/status`, may be absent), server `version`.
6. The profile is written to the report and pinned for the job. Before each attempt the launcher
   re-reads `/v1/models/status`; if the pinned model is no longer loaded or its effective context
   shrank, the attempt does not start (`model_changed`, environment).

`model_fallback: false` stays required: with fallback on, oMLX may serve a different model.

The current CLI/env overrides (`--canonical-model`, `--api-model`, `--context-window`,
`--attempt-timeout`) are replaced by: `--model <id-or-alias>` (selection), `--context-cap <n>`
(an optional *lower* cap; never above the discovered value), and the timing settings in §6.4.

### 6.2 Codex worker invocation (G9, findings 7–8)

- **Isolated home.** Each local job gets `CODEX_HOME=<report_dir>/codex-home` (created fresh).
  The worker never sees the user's `AGENTS.md`, memories, skills, sessions, or `auth.json`.
  `--ephemeral` is passed.
- **Generated model catalog.** The launcher writes `<codex-home>/local-model-catalog.json` with
  one entry for the discovered model (context window, image input, shell type, reasoning, base
  instructions) and passes `-c model_catalog_json=…`. The entry registers no patch tool: oMLX
  passes only `function` tools to the model and drops Codex's freeform `apply_patch`, so edits
  are applied by Codex intercepting the `apply_patch <<'EOF'` heredoc the model runs through the
  shell tool, which the entry's base instructions direct it to use. The exact field values that
  make edits work and keep the tool set suitable for a local model were settled by the catalog
  spike (`docs/qualification/2026-10-24-catalog-spike.md`); the self-check pins the generated
  file's hash.
- **Compaction.** `model_auto_compact_token_limit` is pinned to a fraction of the discovered
  window (builder chooses the fraction; ~80 %), so Codex compacts before the server rejects.
- **Permissions (decided: full host access by default).** Write jobs run Codex with
  `-s danger-full-access` as today: network on, every tool available (simulators, Xcode caches,
  package managers). Codex applies no sandbox in that mode, so the launcher wraps the worker
  process in a **permissive macOS Seatbelt profile** of its own — `(allow default)` plus a short
  list of denials — generated from the permissions list (§6.14):
  - **deny read** of secrets (default list in §6.14), so the model cannot read credentials even
    with full access;
  - **deny write** to the primary checkout's working files and its `.git/HEAD`, `.git/index`
    and `.git/refs/heads`, while the job's own worktree metadata and the shared object store
    stay writable. The worker can still commit inside its worktree (the audit reports that), but
    it cannot change the user's checkout or branches.

  The same profile wraps the launcher's verification commands, native gate, and screenshot
  captures, because they execute code the model wrote. Read jobs additionally use Codex's own
  `-s read-only`. Verified on this Mac (§5): under the wrapper, writes to `$HOME`, DerivedData and
  SwiftPM caches, network, loopback, `simctl`, and git inside the worktree all work; the listed
  secrets are unreadable; writes to the primary checkout's files, `HEAD`, and a new branch are
  denied. A sandboxed worker mode (Codex `workspace-write` with a generated permission profile)
  is reserved in the permissions list for later and not built now.
- **Self-check.** As today, the argv is the whole policy and is checked before launch, but every
  expected value is derived from the job's pinned profile, not from constants.
- **Prompt.** Built from the contract; it states the effective budget and the job kind. Read jobs
  use `--output-schema` with the contract's schema plus the citation envelope (§6.6).

### 6.3 Contract v2 (G2)

One JSON contract; unknown keys refused. Common fields: `task_id`, `kind` (`read` | `write`),
`repo_path`, `base_ref`, `goal`, `timeout_seconds` (job budget), `workstream`,
`environment.pass` (names of host env vars the worker and verification may see, e.g.
`DEVELOPER_DIR`, `JAVA_HOME`; values never logged), `tier` (`local` default, or
`{"cloud": "<slug>", "reasoning_effort": "…"}`).

- `write` adds `acceptance_criteria`, `allowed_paths`, `test_commands`, `max_attempts`, optional
  `native` (§6.5) and `ui_review` (screenshots on/off, defaults on when `native.screenshots`
  exists).
- `read` adds `question`, `scope_paths` (where to look), `answer_schema` (JSON Schema for the
  answer body), `max_answer_bytes`.

The v1 nine-field contract is accepted and upgraded to `kind: write` (compatibility).

### 6.4 Time limits (finding 5)

No fixed per-attempt cap. An attempt ends when it completes, when the job budget is spent, or
when the **stall watchdog** fires: no new worker event for `stall_seconds`. `stall_seconds` is
derived per job from the profile: the time to prefill the worst-case prompt at the measured
prefill rate plus the time to generate `max_output_tokens` at the measured generation rate,
with a floor; when rates are unknown, a conservative floor is used and the report says so.
Verification keeps its reserve from the job budget (unchanged rule, configurable). Every limit
that fired is named in the report with its inputs.

### 6.5 Native Apple verification and screenshots (G6)

Native plan v2 (JSON, referenced by the contract):

- `destination`: a simulator UDID (platform derived from its runtime via `simctl`) or `macos`.
  No platform is hardcoded; `DEVICE_FAMILY = "tvos"` and the tvOS-only runtime filter go away.
- `build`, `install`, `launch`, `test`: argv steps as today (xcodebuild validation kept and made
  platform-neutral).
- `screenshots`: list of `{name, launch_arguments?, url?, settle_seconds?, xcuitest_attachment?}`.
  Each entry is captured either with `simctl io <udid> screenshot` after launching with the given
  arguments/URL, or by exporting a named attachment from the XCUITest result bundle
  (`xcresulttool`) — the latter is the reliable way to reach deep screens such as Settings.
- **Capture sources.** A capture runs against a named source: `before` (the job's base commit),
  `after` (the worker's result), and any number of `reference` sources, each with its own
  `repo_path`/`ref`, scheme or target, and destination — e.g. the original app on another
  branch, scheme or platform. `screenshots/index.md` shows, per screenshot name, one column per
  source.
- **Capture harness.** When the navigation code that reaches a screen (a UI test, launch
  argument handling) only exists in the worker's change, the native plan lists it under
  `capture_overlay_paths`. Those files are copied from the result into the `before` worktree for
  the capture only, and must lie inside test or harness directories the plan declares. A capture
  that still cannot reach the screen is reported as "no before image" with the reason; it never
  fails silently.
- **Capture-only jobs.** `kind: capture` builds and screenshots one or more sources without a
  worker (e.g. the original app before planning). Success is "every requested screenshot exists
  and is not a launch/blank screen"; no code change is expected, so it is never `no_change`.
- For `ui_review` write jobs the launcher captures `before` and `after` (plus any references in
  the plan) and writes `screenshots/index.md`.
- The simulator lease (exclusive per UDID) is kept and applies to every platform.
- The ~1000-line deny-default Seatbelt machinery and the `codex sandbox` verification wrapper are
  deleted (finding 13). They are replaced by the small allow-default wrapper of §6.2, which only
  lists denials and therefore needs no per-service exceptions for the simulator or Xcode.

### 6.6 Read jobs (G2)

`codex exec -s read-only --output-schema <schema>` in a detached worktree at `base_ref` (so the
user's checkout is never touched and the answer is tied to a commit). The answer envelope is
`{answer: <answer_schema>, citations: [{path, line_start, line_end, quote}]}`. The launcher:

1. validates the answer against the schema;
2. verifies every citation: path inside the worktree, line range valid, `quote` present in those
   lines (whitespace-normalised);
3. reports `citations_verified / citations_total`; an answer with a failed citation is a
   capability failure (`unverified_answer`), never passed to the planner as fact.

The planner receives only the validated JSON (small), which is where the token saving comes from.

### 6.7 Failure classification and next action (G3–G5)

`classify(report) -> {class, code, evidence, remediation, next_action}`.

Environment codes (fix the setup, not the model): `server_unreachable`, `server_loading`,
`model_not_loaded`, `model_ambiguous`, `model_changed`, `context_overflow`, `server_memory`
(oMLX memory guard / 507), `tool_unavailable` (e.g. `unsupported call: apply_patch`),
`sandbox_denied` (permission errors from sandboxed commands), `missing_env` / `missing_tool`
(command not found, toolchain variables absent), `simulator_unavailable`, `stall` (watchdog),
`budget_exhausted`, `policy_refused` (self-check), `verification_blocked`, `interrupted`,
`secret_read_denied` (a needed path is on the deny list), `deny_list_unavailable`.

Capability codes (the model could not do it): `tests_failed` after all attempts, `out_of_scope`
(audit violation), `no_change` when a change was required, `invalid_answer` (schema),
`unverified_answer` (citations).

`next_action` is one of `done`, `retry_local` (with the concrete change applied, at most once per
code per job), `escalate_cloud`, `needs_user` (cannot be fixed per job). Each environment code
carries a `remediation` text and, when it is a plugin defect or tuning issue, a
`plugin_suggestion` that `summary` lists under "Suggested fixes for this plugin" so the user is
told. The detection rules use only launcher evidence (exit status, typed events, verification
output, sandbox denial markers), never the model's prose.

### 6.8 Escalation (G4)

- The planner chooses the cloud model. `bin/local-worker models --cloud` prints the live Codex
  catalog (`codex debug models`: slug, description, context window, reasoning levels), so the
  choice is never a hardcoded slug. The skill tells the planner to pick the cheapest model whose
  description fits the job and to prefer low reasoning effort first.
- A cloud job is the same contract with `tier: {"cloud": "<slug>"}` and runs through the same
  pipeline (worktree, audit, verification, screenshots, ledger). Its `CODEX_HOME` is the user's
  (for authentication), with `--ignore-user-config --ignore-rules --ephemeral`.
- Order: local → cloud cheap → cloud stronger → planner implements itself. Each step is a ledger
  entry linked to the job it escalates (`escalates: <job_id>`).
- **Budgets.** Budgets stay keyed by `(repository, workstream)` as today, so renaming a task or
  starting a "new" repair job cannot reset them. The default workstream becomes the `task_id`
  instead of the shared `default`, so unrelated tasks never share a breaker. Budgets are per
  tier: the local tier has the "two consecutive capability failures" breaker and the repair
  allowance; when it trips, `next_action` is `escalate_cloud`, never a block. The cloud tier has
  its own, separate allowance. Only capability failures count; environment failures never do.
  `workstream reset --repo … --workstream …` clears one workstream explicitly and is logged in
  the ledger.

### 6.9 Observability (G7)

- **Live:** `status` (existing) plus a `phase` and `last_event_at` per running job.
- **Per job:** `report.json` (schema-versioned), `changes.patch`, logs (one copy of each stream),
  `screenshots/`, `classification.json`.
- **Session ledger:** `<state>/sessions/<session_id>/ledger.jsonl`, one line per job: id, kind,
  tier, model, profile, attempts, durations (worker, verification, native, screenshots), tokens
  (input, cached, output, reasoning), outcome, class/code, escalation link, report path.
  `session_id` = `CODEX_THREAD_ID` when present, else `--session`.
- **Summary:** `bin/local-worker summary [--session …]` writes `summary.md` + `summary.json`:
  per-job table, per-tier and per-model totals, local share of all tokens, first-try success
  rate, escalations with reasons, screenshots index, suggested plugin fixes, and the **planner's
  own tokens** read from its rollout file (matched by session id; `spawn_agent` children included
  when their rollouts link to it). Missing data is shown as missing, never as zero.
- **Notifications:** the launcher posts a macOS notification (`osascript -e 'display
  notification …'`, no extra dependency) when a job finishes, escalates, needs the user, or
  produces a plugin-fix suggestion, and when `summary` is written. Notifications carry the job
  id, outcome, and one line of reason, never secrets or file contents. On by default, switchable
  off per user (environment variable or setting); failures to notify are logged, never fatal.
  Codex's own `notify` setting is documented as an alternative for users who prefer it.

### 6.10 Robustness (findings 3, 4, 6, 7, 10)

- Signal handlers (TERM, HUP, INT) kill the worker group, write the report as `interrupted`, and
  release leases. Each lease records the worker process group; on start the launcher kills
  orphaned groups from dead launchers before admitting a job.
- The primary-checkout fingerprint becomes informational: the wrapper denies the worker writes to
  the primary checkout's files, `HEAD`, index and branches, so a change made during the job comes
  from the parent or the user. It is reported as "changed during job, not attributed" and does
  not fail the job. A denied write attempt by the worker is reported as an `out_of_scope`
  capability failure with the path. If the wrapper is unavailable (see §6.14), the fingerprint
  stays a failure, as today.
- Archive: fix the dead ref check; add `archive --resume` to reconcile a partial run; bundle with
  the base commit as prerequisite; keep ignored files only in the file copy (size-capped, listed),
  not in git objects; `archive prune --older-than` for old evidence (explicit).
- Output redaction moves to chunk-level replace with a held-back suffix; progress writes are
  throttled.

### 6.11 Plugin, skills, and policy (finding 6, G9)

- Manifest moves to `.codex-plugin/plugin.json` (first-party layout), version read from it by
  every tool (finding 15). Install verified in a throwaway `CODEX_HOME`.
- **Branding** in the manifest `interface`, as first-party plugins do: `displayName`,
  `shortDescription`, `longDescription`, `developerName`, `category`, `capabilities`,
  `brandColor` (+ dark), `logo` (+ dark), `composerIcon`, `screenshots`, and `defaultPrompt`
  starter prompts (e.g. "Implement this with local workers and give me the full statistics").
  Assets live in `assets/`; the builder designs a simple original icon (no third-party marks).
- No global auto-push or "never implement" policy ships with the plugin any more:
  `global-policy.md` and the managed block's completion section are removed from the package.
  Setup installs at most a short, opt-in `AGENTS.md` pointer to the skill.
- **The user's completion rule stays the user's** (decided). The commit / push / worktree-cleanup
  rule is the user's own text and lives in `~/.codex/AGENTS.md` outside any plugin marker; the
  plugin never ships, edits, or deletes it. Migration of an existing install: `--apply` replaces
  the old managed block (and an exact copy of the historical policy) with (a) the user's
  completion rule as plain text outside the markers, byte-for-byte as currently installed
  (including the 198cbc1 wording), and (b) the opt-in pointer block if the user asked for it,
  else no block. A backup is written first, as today; a second `--apply` is a no-op. If the
  user's file already holds that rule outside the markers, nothing is duplicated. `--check`
  reports what would move before anything is written.
- The `local-worker` skill teaches the planner the loop: discover → read jobs → plan → write jobs
  → review (patch, tests, screenshots) → follow `next_action` → `summary` at the end, and to
  surface "Suggested fixes for this plugin" to the user.
- `doctor` runs discovery and reports the live profile, sandbox readiness, simulator list, and
  Codex version compatibility.

### 6.12 Per-task MCP tools

The existing per-task tool manifest (`--tool-config`, schema 1: named credentials, explicit
`enabled_tools`, probe before inference, `configured`/`probed`/`observed` evidence) stays as a
feature and keeps its own file and flag rather than moving into the contract. In the new layout
it is passed into the job's isolated `CODEX_HOME` through the same `-c` overrides, the probe uses
that same isolated home, and the self-check covers it as today. Whether MCP servers that Codex
spawns are subject to the worker's sandbox (stdio servers needing files, HTTP servers needing
network) is settled in the plan's catalog/sandbox spike; if they are, a tools manifest may declare
the writable roots or network access its servers need, recorded in the report.

### 6.13 Dashboard widget (later phase, Codex desktop only)

A live dashboard rendered in the Codex desktop conversation through the MCP Apps standard, as
OpenAI's own `creative-production` plugin does (local MCP server, `ui://` HTML resource with MIME
`text/html;profile=mcp-app`, host bridge `ui/*` messages, display modes `inline`, `fullscreen`,
`pip`). Codex offers no way to add labels or icons to the desktop side panel; `pip` (floating)
is the closest surface.

- **Content:** the session's jobs (kind, tier, model, phase, outcome, durations), token totals
  local vs cloud and per job, escalations with reasons, suggested plugin fixes, and before /
  after / reference screenshots side by side. Live while jobs run, final after `summary`.
- **Source of truth:** the session ledger and job reports (§6.9). The widget only reads them; it
  never starts, stops, or changes jobs in its first version.
- **Server:** a small stdio MCP server shipped by the plugin (`.mcp.json`), with read-only tools
  such as `open_dashboard(session)` and `job_details(job_id)`. Stdlib Python is preferred; the
  widget's HTML and the MCP Apps bridge script are static files in the package.
- **Spike first (gate for this phase):** prove in the installed Codex desktop app that a locally
  served widget renders, which display modes the host actually offers, how screenshots can be
  shown (embedded data vs resource), how updates reach a mounted widget, and that the CLI simply
  ignores it. If the spike fails, this phase is dropped and nothing else depends on it.
- The phase comes after the core redesign; no other part of the design depends on it.

### 6.14 Permissions list

One user-owned file, `<state>/permissions.json` (schema-versioned), is the single source of what
every background worker may do. Workers never inherit approvals from an interactive session,
which is what makes background terminals lose access to folders the user approved elsewhere.

- `mode`: `full_host` (default, built now) or `sandboxed` (reserved; refused until implemented).
- `deny_read`: paths the worker and launcher-run steps cannot read. Default list (builder
  finalises): `~/.codex` (local-tier jobs use an isolated home), `~/.ssh`, `~/.omlx/settings.json`,
  `~/.aws`, `~/.config/gh`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`,
  `~/.npmrc`, `~/.pypirc`, `~/.gnupg`, browser profile directories, and password-manager data.
  Keychains are not denied by default (code signing may need them); the user can add them.
- `protect_primary_checkout`: on by default (the write denials of §6.2).
- `writable_roots`, `network.allowed_domains`: reserved for `sandboxed` mode; stored and shown,
  not yet applied.
- Commands: `local-worker permissions show`, `deny-read <path>`, `allow-read <path>` (remove from
  the deny list), each logged in the ledger. The planner can show suggestions but the skill tells
  it never to edit this file itself; changes are the user's.
- Preflight refuses a list that would hide the repository, the worktree root, the state
  directory, or the toolchain (e.g. a deny entry above the repo path), naming the conflict.
- A worker command denied by the wrapper is classified `secret_read_denied` or `out_of_scope`
  with the exact path, so a needed path is one explicit `allow-read` away, and the summary lists
  it under suggested fixes.
- Cloud-tier jobs need Codex's own authentication, which a deny on `~/.codex` would block for
  the Codex process too (Seatbelt cannot tell the parent from its children). The plan's spike
  decides between a job-scoped `CODEX_HOME` containing only what authentication needs and
  leaving the auth file readable for cloud jobs; the choice is recorded in each cloud report.
- `sandbox-exec` is deprecated by Apple but works on this macOS. `doctor` and preflight check it;
  if it is missing, jobs still run with full host access, the report says the deny list was not
  applied, and the user is notified. That is never silent.

## 7. Edge cases that must be handled

- oMLX not running / port moved / key rotated / health 503 while preloading.
- No model loaded; several loaded; a helper or embedding model loaded alongside; the model
  unloaded or swapped mid-job; alias added or removed between jobs.
- Effective context lowered after load (engine memory plan); prompt above it (`context_overflow`).
- oMLX memory guard / 507; another client saturating the server (`waiting_requests`).
- Thinking models spending most output on reasoning; reasoning tokens reported separately.
- Codex unknown-model fallback metadata; missing patch tool; Codex version drift (flags or
  catalog fields absent) — preflight checks the Codex version and features it depends on.
- Worker produces no events for a long time (stall vs slow prefill); worker floods output.
- Launcher killed; terminal closed; machine sleeps mid-job (monotonic deadline vs wall clock).
- Sandbox denial inside the worker; verification needs env vars or tools the sandbox lacks.
- Read job: schema-invalid JSON, citations to non-existent lines, answer larger than allowed.
- Write job: out-of-scope edits, commits by the worker, edits reverted by tests, no change.
- Native: simulator busy/booted by another session, runtime missing, app fails to launch,
  XCUITest attachment missing, screenshot of a black/launch screen (settle time).
- Cloud escalation: not logged in, model slug no longer in catalog, cloud worker also fails.
- Two Macs: different models, ports, Xcode versions — all discovered, none configured.

## 8. Testing and qualification

- Unit tests (offline, as today) for every module, with **recorded oMLX fixtures** captured from
  the real server: `/health`, `/api/status`, `/v1/models/status` (one model, several, none,
  loading, helper present), streamed `/v1/responses`, and a real `codex exec --json` stream.
- Contract tests for Codex: generated catalog accepted, and the tools and base instructions Codex
  really sends checked by pointing the worker at a loopback recording stub that captures the
  request (the catalog spike's `render`/`calls` method; `codex debug prompt-input` renders the
  input list only and never shows tools); argv flags exist in the installed version.
- Live qualification suite (`tests/live/`, opt-in, run on both Macs): discovery, read pilot with
  citations, write pilot, forced environment failure (context cap too small) and its
  classification, forced capability failure and a cloud escalation dry run (catalog + argv only
  unless the user allows a real cloud call), native pilot on any available simulator with
  before/after screenshots, `summary` output. Results are recorded in `docs/qualification/`.

## 9. Compatibility and migration

- State root `~/.local-implement` and env names stay (pool continuity).
- v1 contracts accepted. Existing reports remain readable by `status`/`archive`.
- The interim CLI/env overrides from 2026-10-24 (uncommitted) are replaced, not kept.

## 10. Constraints

- Stdlib only; Python 3.9 (Xcode). macOS only.
- Loopback-only model endpoint; API key never in argv, logs, prompts, or reports.
- Codex CLI 0.164+ (minimum recorded by preflight).

## 11. What the planner sees (contract between skill and launcher)

Every `run` prints one JSON object (with `--json`) whose stable top-level keys are: `job_id`,
`kind`, `tier`, `model`, `status`, `classification` (`class`, `code`, `remediation`,
`next_action`, `plugin_suggestion`), `answer` (read jobs, validated), `patch_path`,
`screenshots` (paths), `tests`, `tokens`, `durations`, `report_path`. The skill relies only on
these keys.

## 12. Review findings → design

| # | Finding | Addressed by |
| --- | --- | --- |
| 1 | Model/alias hardcoded | §6.1 discovery |
| 2 | Context window hardcoded | §6.1 effective window, §6.2 compaction |
| 3 | Circuit breaker bricks default workstream | §6.8 per-chain, capability-only, reset |
| 4 | Orphaned worker on SIGTERM | §6.10 signals + orphan reaping |
| 5 | Fixed 300 s attempt cap | §6.4 stall watchdog |
| 6 | Global AGENTS.md routing + auto-push | §6.11 |
| 7 | Primary fingerprint false violations | §6.2 write denials, §6.10 informational |
| 8 | Worker uses real HOME/CODEX_HOME, unconfined | §6.2 isolated home, deny-read list (§6.14) |
| 9 | Fixed env allowlist | §6.3 `environment.pass` |
| 10 | Archive dead check / unrecoverable partial | §6.10 |
| 11 | Archive repo bloat | §6.10 |
| 12 | Per-character redactor | §6.10 |
| 13 | Dead legacy backends | §6.5 deletion |
| 14 | Stream parsed/stored twice | §6 `events.py`, one log copy |
| 15 | Version duplicated | §6.11 |

## 13. Decisions

Decided by the user:

- oMLX only for now; nothing model-specific hardcoded; works on both Macs as-is.
- Tasks always finish; failures are analysed; environment problems become fixes and are told to
  the user; capability problems escalate to the cheapest suitable cloud model chosen by the
  planner.
- Local model handles reading as well as writing, to save cloud tokens.
- Screenshots for UI changes, provided to the user.
- Full statistics at the end (agents, workers, tokens).
- Native Apple support stays, but not tvOS-specific.
- The commit / push / worktree-cleanup rule stays as the user's own text in
  `~/.codex/AGENTS.md`; the plugin no longer ships or manages it (§6.11).
- Workers keep full host access by default; secrets are hidden by a deny-read list; a
  user-owned permissions list exists now and reserves a sandboxed mode for later (§6.2, §6.14).
- An MCP Apps dashboard widget (jobs, tokens, screenshots, escalations) as a later phase with a
  spike first (§6.13), plus manifest branding (§6.11) and macOS notifications (§6.9).

My calls (check while reading):

- Selection rule when several models are loaded (§6.1.4) and explicit `--load`.
- Capture sources (before / after / any reference), capture harness overlay, and capture-only
  jobs (§6.5).
- Budgets per `(repository, workstream)` and per tier, default workstream = task id, breaker
  leads to escalation, never to a block (§6.8).
- Per-task MCP tool manifests kept and carried into the isolated home (§6.12).
- Cloud workers run through the same launcher pipeline instead of `spawn_agent` (§6.8), so audit,
  verification, screenshots and statistics are uniform.
- Stall watchdog instead of a per-attempt cap (§6.4).
- Read jobs require verifiable citations (§6.6).
- Tokens only, no prices (§3).
- Manifest moves to `.codex-plugin/plugin.json`; global policy reduced to an opt-in pointer (§6.11).
- Package restructure into modules (§6), moving working code rather than rewriting it.

Open: none.
