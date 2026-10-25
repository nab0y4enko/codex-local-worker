# Catalog spike: a Codex model entry for the local model (2026-10-24)

Plan task 6, the decision gate for spec §6.2 "Generated model catalog". The question: which
`-c model_catalog_json=…` entry makes Codex treat the local oMLX model well? The answer is now
`src/codex_local_model_template.json`. Task 7 fills it in for each job.

**Decision: a variant passes.** The shipped template passed the live gate in 13 of 13 real
pilot jobs. Codex reported no `Model metadata … not found` item, logged no
`unsupported call: apply_patch`, applied every edit with its own patch engine (a completed
`file_change` item), and the launcher's verification passed. Task 7 ships a generated catalog,
not `catalog: "fallback"`. Task 7 then revised the base instructions (allow project tools and
`git mv`, document `*** Move to:`, let a required answer format win) and re-ran the gate on the
launcher's own generated catalog: 3 of 3 passed (see "Task 7: revised instructions").

## Environment

| | |
| --- | --- |
| Codex CLI | `codex-cli 0.164.0` (`/opt/homebrew/bin/codex`) |
| oMLX | 0.7.3.dev1 on `127.0.0.1:8000`, Responses API |
| Model | `Qwen3.5-9B-OptiQ-4bit`, `engine_type: vlm`, thinking on by default |
| Limits (`bin/local-worker preflight`) | effective window 262144, max output 32768 |
| Codex home | a fresh throwaway `CODEX_HOME` under `<scratch>` for every Codex process; `~/.codex` was never used |
| Inference | the loopback oMLX only; no cloud model was called |

## Reproduce

All commands run from the repository root with `PYTHONDONTWRITEBYTECODE=1`. Only `pilot` runs
inference.

```sh
python3 scripts/spike_local_catalog.py variants                      # list the variants
python3 scripts/spike_local_catalog.py schema --out <scratch>/final/schema
python3 scripts/spike_local_catalog.py render --out <scratch>/final/render
python3 scripts/spike_local_catalog.py calls  --out <scratch>/final/calls \
    --variant none --variant template --variant template-freeform --variant truncation-tokens
python3 scripts/spike_local_catalog.py pilot  --out <scratch>/final/pilot-direct \
    --variant none --variant template --repeat 3
python3 scripts/spike_local_catalog.py pilot  --out <scratch>/final/pilot-record --record \
    --variant template --variant template-freeform --variant template-fallback-instructions \
    --variant instructions-empty --variant responses-lite
```

- **`schema`** runs `codex debug prompt-input` with the worker's own `-c` overrides plus
  `-c model_catalog_json=<file>`. It finds the required fields by loading `{"slug": …}` and
  adding each field the parser reports as missing. It finds the accepted values by loading
  deliberately wrong ones, so the parser lists the right spellings.
- **`render`** builds the launcher's exact worker argv (`local_worker.worker_argv`) and adds the
  variant's catalog. It points the provider at a **loopback recording stub** that answers every
  Responses request with a canned "ok", so it captures what Codex really sends: base
  instructions, tools and reasoning settings. It also renders `codex debug prompt-input` with the
  same overrides.
- **`calls`** uses the same stub, but it answers the first request with a scripted tool call:
  each way a model can ask for a patch, plus a 60,000-character command output.
- **`pilot`** builds a fresh pilot (`scripts/prepare_pilot.py`, `timeout_seconds` raised to 900)
  and runs the real launcher, `bin/local-worker run --task-file … --state-dir … --json`.
  Since Task 7 the launcher generates and passes the catalog itself, so the `template` variant
  runs on the launcher's own catalog. For every other variant `--codex-bin` points at a
  generated wrapper that drops the launcher's `-c model_catalog_json=…` and adds the variant's
  before the trailing `-- -` (`none` adds none), so a worker never gets two catalogs. (Before
  Task 7 the wrapper only added the variant's catalog, because the launcher passed none.) The
  launcher reads the oMLX key itself. With `--record`, the wrapper also points
  the provider at a loopback proxy in front of oMLX. The proxy forwards everything unchanged,
  records the request bodies and finished response items, and never records `Authorization`.
  The proxy is transparent: `pilot-direct` runs without it.

`variants` lists every variant. In brief: `none` is no catalog (Codex fallback metadata).
`cloud-clone` is the first entry of `codex debug models` with the slug and window swapped.
`minimal` is the required fields only. `base` is minimal plus window, modalities,
`apply_patch_tool_type: freeform` and `tool_mode: direct`. The one-factor variants each change
one field of `base`. `template*` variants are the shipped file, or the file with one field
changed.

## Why the fallback fails

With no catalog, Codex 0.164 emits the error item
``Model metadata for `Qwen3.5-9B-OptiQ-4bit` not found. Defaulting to fallback metadata…`` and
uses fallback metadata:

- **Base instructions:** the 16,979-character open-source Codex CLI prompt ("You are a coding
  agent running in the Codex CLI…"). It says "Use the `apply_patch` tool to edit files" and shows
  the old `{"command":["apply_patch", …]}` call shape.
- **Tools:** `exec_command`, `write_stdin`, `request_user_input`, `view_image`, `get_goal`,
  `create_goal` and `update_goal`. There is **no** `apply_patch` tool.

So the model calls a function named `apply_patch`. In every recorded run its arguments were
`{"patch": "*** Begin Patch…"}`. That tool does not exist, so Codex logs
`ERROR codex_core::tools::router: error=unsupported call: apply_patch` and answers
`unsupported call: apply_patch`. The model then edits with `sed -i`, which fails because BSD
sed needs an argument after `-i`, or with `echo >`/`printf >` rewrites. This happened in 7 of 7
`none` runs.

## Schema (from `schema`)

- **Required to load:** `slug`, `display_name`, `supported_reasoning_levels`, `shell_type`,
  `visibility`, `supported_in_api`, `priority`, `support_verbosity`, `truncation_policy`,
  `experimental_supported_tools`, and either `base_instructions` or
  `model_messages.instructions_template`. An empty `base_instructions` (`""`) loads.
- **Accepted values:**
  - `apply_patch_tool_type`: `freeform` only, or `null`.
  - `shell_type`: `default`, `local`, `shell_command`, `unified_exec` or `disabled`.
  - `truncation_policy.mode`: `bytes` or `tokens`.
  - `input_modalities`: `text`, `image` or `audio`.
  - `visibility`: `list`, `hide` or `none`.
  - `web_search_tool_type`: `text` or `text_and_image`.
  - `default_verbosity`: `low`, `medium` or `high`.
  - `default_reasoning_summary`: `auto`, `concise`, `detailed` or `none`.
  - `tool_mode` and `multi_agent_version`: any string loads. The binary's enum strings are
    `direct` / `code_mode_only` and `v1` / `v2`.
- Every cloud entry from `codex debug models` uses `tool_mode: code_mode_only`,
  `apply_patch_tool_type: freeform`, `shell_type: unified_exec` and `use_responses_lite: true`.
- `supports_parallel_tool_calls` is not a 0.164 field. It is ignored: the request still sends
  `parallel_tool_calls: true`.

## What actually reaches the model: oMLX drops non-function tools

oMLX 0.7.3 converts Responses tools to chat-template tools in
`/Applications/oMLX.app/Contents/Resources/omlx/api/responses_utils.py`:

- **`convert_responses_tools` keeps only two kinds of tool:** top-level `function` tools and the
  `function` members of `namespace` tools. It silently drops every other type, including
  `custom`, which is how Codex sends the freeform `apply_patch` (with a Lark grammar).
- **`convert_responses_input_to_messages` handles only four input item types:** `message`,
  `reasoning`, `function_call` and `function_call_output`. `use_responses_lite: true` moves the
  whole tool list into an `additional_tools` input item, so the model gets **no tools at all**.
- **Only `function_call` items come back.** oMLX never emits `custom_tool_call`.

The freeform patch tool therefore cannot reach this model through oMLX, whatever the catalog
says. The working patch path is a different one. Codex **intercepts** `apply_patch <<'EOF' … EOF`
run through `exec_command` and applies it with its own patch engine. That produces a completed
`file_change` item, with no shell `apply_patch` binary involved. This works with or without a
catalog (see `calls`).

## Step 1: tool lists per variant (`render`, no inference)

"Offered" is what Codex sent. "Model sees" is what oMLX passes on, per the filter above.
Instruction sizes are in characters. The static gate passes when Codex loaded the entry, the
model sees a shell tool, no code-mode or multi-agent tool is offered, and the base instructions
are not the cloud ones.

| Variant | Offered | Model sees | Base instr. | Static gate |
| --- | --- | --- | ---: | --- |
| `none` | exec_command, write_stdin, request_user_input, view_image, get/create/update_goal | same | 16,979 | fail: fallback metadata |
| `cloud-clone` | all in an `additional_tools` input item: exec [custom], wait, request_user_input(_async), sleep, followup_task, interrupt_agent, list_agents, send_message, spawn_agent, wait_agent | (none) | 21,420 (cloud) | fail: input-item tools, code-mode/multi-agent tools, cloud instructions |
| `minimal` | as `none` | as `none` | 16,979 | pass |
| `base` | as `none` + apply_patch [custom] | as `none` | 16,979 | pass |
| `tool-mode-code` | exec [custom], wait, request_user_input | wait, request_user_input | 16,979 | fail: no shell tool, code-mode tools |
| `tool-mode-unset` | same as `base` | as `none` | 16,979 | pass |
| `no-patch-tool` | as `none` | as `none` | 16,979 | pass |
| `shell-unified` / `shell-default` / `shell-local` | same as `base` (shell_type changes nothing) | as `none` | 16,979 | pass |
| `shell-disabled` | `base` without exec_command / write_stdin | no shell tool | 16,979 | fail: no shell tool |
| `instructions-empty` | same as `base` | as `none` | 0 | pass |
| `truncation-tokens` | same as `base` | as `none` | 16,979 | pass |
| `text-only` | same as `base` (view_image stays) | as `none` | 16,979 | pass |
| `responses-lite` | all in an `additional_tools` input item | (none) | 16,979 | fail: input-item tools |
| `template` | as `none` | as `none` | 2,084 | pass |
| `template-freeform` | as `none` + apply_patch [custom] | as `none` | 2,084 | pass |
| `template-fallback-instructions` | as `none` | as `none` | 16,979 | pass |

Other observations:

- **Reasoning settings.** Every non-lite variant sends `reasoning: {"summary": "auto"}` and
  `parallel_tool_calls: true`. The cloud clone sends `reasoning.effort: "low"`,
  `text.verbosity: "low"` and `parallel_tool_calls: false`.
- **`codex debug prompt-input` is not a view of what the model gets.** It renders the input list
  only. It never shows tools, and it shows base instructions only when `use_responses_lite` puts
  them in a developer message. Without lite, Codex still sends them as the first developer
  message, which the stub captured. Without a catalog it showed three items: the skills
  developer message, the environment context and the prompt.
- **The static gate cannot choose among the passing variants.** They differ only in base
  instructions and in a freeform tool the model never sees. The live gate decides.

## How Codex handles each patch request (`calls`, scripted, no inference)

| Catalog | Model emits | File fixed | `file_change` | Codex answers the model | stderr |
| --- | --- | --- | --- | --- | --- |
| `none` / `template` | function `apply_patch` (`{input}`, `{patch}` or `{command}`) | no | no | `unsupported call: apply_patch` | `unsupported call: apply_patch` |
| `template-freeform` | function `apply_patch` (any of the three) | no | no | `aborted` | `Fatal error: tool apply_patch invoked with incompatible payload` (the turn continues) |
| any | `exec_command` with `apply_patch <<'EOF' … EOF` | yes | yes (completed) | `Success. Updated the following files: M add.py` | none |
| `none` / `template` | `custom_tool_call apply_patch` (control; oMLX never emits it) | no | no | `unsupported custom tool call: apply_patch` | same |
| `template-freeform` | `custom_tool_call apply_patch` (control) | yes | yes | success | none |

On truncation, a 60,000-character command output reached the model as 10,208 characters with
`bytes`/10000. That matches the fallback. With `tokens`/10000 it reached the model as 40,208
characters, about 4x more prompt to prefill at the server's measured 241 tokens/s.

## Step 2: pilot jobs (real local inference)

The pilot task is to fix `add()` in `add.py`, which has only that path allowed. `sentinel.txt`
is dirty in the primary checkout. Each run used a fresh pilot repository and a fresh
`CODEX_HOME`, and ran the real launcher. The live gate requires all four of these:

- no `Model metadata … not found` item;
- no `unsupported call` on stderr;
- a completed `file_change` item;
- launcher `status: ok`.

### Final runs, committed template

The worker column is in seconds. Tokens are input / cached / output.

| Run | Variant | Status, tests | Worker | Tokens | Patch tool applied | `unsupported call` | Metadata error | Gate |
| --- | --- | --- | ---: | --- | --- | ---: | --- | --- |
| direct 1 | `none` | ok, 0 | 34.3 | 72,852 / 68,272 / 751 | no (shell edit) | 1 | yes | fail |
| direct 2 | `none` | ok, 0 | 39.8 | 74,871 / 69,854 / 964 | no | 2 | yes | fail |
| direct 3 | `none` | ok, 0 | 33.5 | 64,733 / 60,188 / 786 | no | 2 | yes | fail |
| direct 1 | `template` | ok, 0 | 22.8 | 23,646 / 20,651 / 506 | yes | 0 | no | **pass** |
| direct 2 | `template` | ok, 0 | 21.8 | 23,696 / 20,668 / 512 | yes | 0 | no | **pass** |
| direct 3 | `template` | ok, 0 | 21.8 | 23,644 / 20,650 / 487 | yes | 0 | no | **pass** |
| record | `template` | ok, 0 | 20.6 | 23,697 / 20,696 / 471 | yes | 0 | no | **pass** |
| record | `template-freeform` | ok, 0 | 20.7 | 23,795 / 20,766 / 450 | yes | 0 | no | pass |
| record | `template-fallback-instructions` | ok, 0 | 47.0 | 66,331 / 61,264 / 1,323 | no | 2 | no | fail |
| record | `instructions-empty` | ok, 0 | 18.5 | 20,906 / 18,471 / 426 | no (rewrote the file with `cat > add.py <<'EOF'`) | 0 | no | fail |
| record | `responses-lite` | tests_failed, 1 | 17.8 | 5,575 / 2,048 / 304 | no (the model saw no tools and wrote `<tool_call>` text into its answer) | 0 | no | fail |

The recorded `template` runs made only `exec_command` calls, for example `cat add.py`, then
`apply_patch <<'EOF'` with a bare `@@` hunk, then `python3 -m unittest -v`. The only remaining
error item in every run is ``Under-development features enabled: skip_host_skill_discovery``,
which comes from the launcher's feature pins, not the catalog. (Task 7 dropped that pin; see
"Findings outside the catalog".)

### All runs, by variant

These totals cover the final runs and earlier ones in `<scratch>`. The earlier `template` runs
used the same request: the request body Codex sends for the committed file is byte-identical
(ids and paths aside) to the one those runs used. The committed file only drops an ignored
field and spells out three defaults.

| Variant | Runs | Gate pass | Patch applied | Metadata error | `unsupported call` | Worker s, median (range) | Input tokens, median (range) |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `none` | 7 | 0 | 0 | 7 | 10 | 34.2 (26.4–48.9) | 58,926 (47,247–74,871) |
| `template` | 13 | **13** | 13 | 0 | 0 | 22.7 (18.3–28.5) | 23,664 (18,569–39,475) |
| `template-freeform` | 4 | 4 | 4 | 0 | 0 | 21.3 (18.1–26.0) | 23,714 (18,560–29,065) |
| `base` (fallback instructions + freeform) | 1 | 0 | 0 | 0 | 0 | 28.8 | 47,682 |
| `template-fallback-instructions` | 2 | 0 | 0 | 0 | 4 | 35.9, 47.0 | 73,324, 66,331 |
| `instructions-empty` | 2 | 0 | 0 | 0 | 0 | 43.6, 18.5 | 27,551, 20,906 |
| `responses-lite` | 2 | 0 | 0 | 0 | 0 | 18.4, 17.8 | 5,579, 5,575 |

### Instruction drafts that did not ship

| Draft | Instructions sha256, length | Runs | Passed | What went wrong |
| --- | --- | ---: | ---: | --- |
| First draft (template and freeform) | `afc996ec`, 2,163 chars | 4 | 4 | One freeform run sent four malformed hunks (unified-diff headers such as `@@ -1,6 +1,6 @@`) before one applied. |
| Second draft: added "never a line-number range: do not write `@@ -1,5 +1,5 @@`" | `fa1e56ec`, 2,305 chars | 3 | 1 | The negative example primed the 9B model. Two runs wrote exactly those headers: one applied after seven failed patches, the other gave up and rewrote the file with `printf`. The third called a function `apply_patch`. |
| Third draft, shipped by the spike | `6b445a43`, 2,084 chars | 13 | 13 | — |
| Task 7 revision of the third draft (shipped now) | `0ad2098b`, 2,532 chars | 3 | 3 | — (see "Task 7: revised instructions") |

The hashes are the first 8 hex digits of the sha256 of the `base_instructions` string, the value
`render` reports as `base_instructions_sha256`.

The shipped instructions give only a positive example with a bare `@@` hunk and context lines,
and never name a tool called `apply_patch`.

**Limits.** Every qualifying run, for all drafts and variants, was the same pilot: one trivial
task whose whole edit is a one-line `*** Update File` hunk in `add.py`. The shipped text is the
third draft, tuned on that pilot after the first two drafts were judged on it, so it is fitted
to that task as much as it is qualified. Nothing here shows how the model handles
`*** Add File`, `*** Delete File`, `*** Move to:`, several hunks in one file or several files in
one patch. The live qualification suite (plan Task 26) must cover Add File, Delete File,
multi-hunk and multi-file edits before the instructions count as qualified beyond this pilot.

### Task 7: revised instructions

Task 7 changed the third draft minimally (`6b445a43`, 2,084 chars, to `0ad2098b`, 2,532 chars):

- Hand edits still go through the `apply_patch` heredoc via `exec_command`, and `sed -i`,
  `echo >`, `cat >` and self-written scripts are still not allowed for them. New: when the task
  calls for them, the model may run the project's own formatters, code generators and build
  tools, and move a file with `git mv`. The old text forbade any scripted edit, which ruled out
  the formatters and generators real tasks need.
- The patch format adds `*** Move to: NEW_PATH` right after `*** Update File: PATH`, followed by
  the hunks, and says to use `git mv` for a move without changes. A `calls`-style stub check
  confirmed that Codex's patch engine applies a move with a hunk. A move with no hunk is
  refused: ``Update file hunk for path 'add.py' is empty``.
- "Finish with a short summary" now yields when the task specifies the format of the final
  answer, so it does not conflict with a JSON answer under `--output-schema`.

The gate was re-run with the existing script and local inference only:
`pilot --variant template --repeat 3`. Each run used a fresh pilot. The launcher ran with its
Task 7 invocation: generated catalog, job `CODEX_HOME`, `--ephemeral`, and the pins described
under "Findings outside the catalog". Tokens are input / cached / output.

| Run | Status, tests | Worker s | Job wall s | Tokens | Patch tool applied | `unsupported call` | Error items | Gate |
| --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- |
| 1 | ok, 0 | 27.1 | 28.4 | 16,379 / 12,764 / 531 | yes | 0 | 0 | **pass** |
| 2 | ok, 0 | 18.2 | 20.0 | 16,360 / 14,801 / 512 | yes | 0 | 0 | **pass** |
| 3 | ok, 0 | 17.6 | 18.8 | 16,332 / 14,778 / 501 | yes | 0 | 0 | **pass** |

All three used the launcher's own catalog (the same `catalog_sha256` each time) and ran
`cat add.py`, then the patch, then `python3 -m unittest -v`. Input tokens dropped from about
23,700 to about 16,350 per job. That drop comes from the new pins, not the wording: the
3,458-character skills message and the three goal tools are no longer sent. A `render` of the
launcher's argv confirms the request now carries just two developer messages, the
2,532-character base instructions and the permissions note, plus `exec_command`, `write_stdin`,
`request_user_input` and `view_image`. The revised wording keeps the pass rate on this pilot
(3 of 3), and the Limits above still apply to it.

## Decision: the template, field by field

`src/codex_local_model_template.json`. Placeholders are whole JSON strings, and Task 7
substitutes typed values: `"{{slug}}"` and `"{{display_name}}"` become strings,
`"{{context_window}}"` becomes an integer, and `"{{input_modalities}}"` becomes a list such as
`["text", "image"]` for a VLM or `["text"]` otherwise. `scripts/spike_local_catalog.py`
(`fill_template`) is a reference filler.

| Field | Value | Why |
| --- | --- | --- |
| `slug`, `display_name` | `{{slug}}`, `{{display_name}}` | The discovered `api_model`. A matching slug is what removes the "metadata not found" fallback. |
| `description` | "Local model served by oMLX on loopback." | Informational. |
| `context_window` | `{{context_window}}` | The discovered effective window. The launcher keeps pinning `model_context_window` and the compaction limit itself. |
| `input_modalities` | `{{input_modalities}}` | From `accepts_images`. This does not change the tool list (view_image is a feature). |
| `shell_type` | `unified_exec` | Matches the shell tool Codex actually offers (`exec_command` / `write_stdin`). In 0.164 only `disabled` changed the tool list. |
| `apply_patch_tool_type` | `null` | oMLX drops the freeform tool before the model sees it. Without it, a stray `apply_patch` function call gets an actionable `unsupported call: apply_patch`; with it, Codex answers `aborted` and logs a `Fatal error`. Both pass the live gate (13/13 and 4/4). |
| `tool_mode` | `direct` | `code_mode_only` replaces every tool with a custom `exec` (JavaScript) tool plus `wait`, so the model has no shell. |
| `use_responses_lite` | `false` | `true` moves all tools into an `additional_tools` input item that oMLX ignores: no tools, and the job failed. |
| `truncation_policy` | `bytes`, 10000 | Same as the fallback. `tokens`/10000 sends about 4x more output back to a slow-prefill local model. |
| `supported_reasoning_levels` | `[]` | oMLX reports `reasoning_efforts: null` for this model. Codex then sends no effort, only `summary: auto`. |
| `support_verbosity` | `false` | No `text.verbosity` is sent. |
| `experimental_supported_tools` | `[]` | No cloud experimental tools. |
| `include_{skills,plugin,apps}_usage_instructions` | `false` | Explicit defaults. `true` adds about 2.5k characters of skills guidance. |
| `visibility`, `supported_in_api`, `priority` | `list`, `true`, `0` | Required; neutral values. |
| `base_instructions` | 2,532 characters, local (2,084 as qualified by the spike) | States the unattended context, the shell tool, and how to edit: an `apply_patch` heredoc through `exec_command`. Explains the patch format with one positive example and a bare `@@` hunk, plus `*** Move to:`. Forbids `sed -i` and redirection edits by hand; allows the project's own formatters, generators and build tools and `git mv` (Task 7). Ends with working rules. Against the fallback prompt it uses 60% fewer input tokens per job (median 23,664 vs 58,926), and the worker finishes about 34% faster (median 22.7 s vs 34.2 s). |

Task 7 should hash the generated file into the argv self-check, as spec §6.2 says. (Done: the
launcher writes the catalog into the job's `CODEX_HOME`, records its `catalog_sha256`, and
re-hashes the file in the self-check before every launch.)

## Findings outside the catalog (for later tasks)

- **`get_goal` / `create_goal` / `update_goal` are offered to every worker.** Pinning
  `features.goals=false` removes them (checked with `render --set features.goals=false`).
  Task 7 pins it.
- **`request_user_input` is always offered by `codex exec` 0.164.** It is described as
  Plan-mode only. No catalog field or feature removed it.
- **`view_image` stays with a text-only entry.** Only the `view_image` feature controls it.
- **Host skills leak into the worker prompt.** Even with a throwaway `CODEX_HOME` and
  `features.skip_host_skill_discovery=true`, the worker's skills developer message (about
  4,000 characters) lists `~/.agents/skills` as skill root `r0`. It also lists the system skills
  that Codex installs into the throwaway home (`skills/.system`: imagegen, openai-docs, …).
  Spec §6.2 says the worker never sees the user's skills, so the isolation work needs a way to
  turn this off.

  **Resolved in Task 7.** The binary's config schema has a `skills` table with `bundled`,
  `include_instructions`, `max_context_tokens` and `config`, plus the `skill_search` and
  `skip_host_skill_discovery` features. Each candidate was tried with the request recorder, with
  no inference:
  - **No switch in Codex 0.164 turns off discovery of `~/.agents/skills` itself.**
    `features.skip_host_skill_discovery` true and false give the same skills message.
    `features.skill_search=false` changes nothing.
  - `skills.include_instructions=false` drops the whole skills developer message (3,458
    characters here), so the request names no skill root and no skill.
  - `skills.bundled.enabled=false` stops Codex installing the system skills into the job home.

  Task 7 pins both `skills.*` values and the self-check requires them. Per-skill
  `skills.config` entries could disable named skills, but only by listing each host's skills,
  so they were not used.
- **Every run carries one error item:** ``Under-development features enabled:
  skip_host_skill_discovery``. Either pin `suppress_unstable_features_warning=true` or drop
  that feature pin; otherwise every report keeps an error item that is not a real error.
  **Resolved in Task 7:** the pin achieved no isolation (above), so it was dropped. No
  under-development feature is enabled now, so the error item is gone (0 in the three Task 7
  runs) and `suppress_unstable_features_warning` is not needed.
- **Codex writes into its home during a run.** Besides its state databases, Codex 0.164 adds a
  `[projects."<repo>"] trust_level = "trusted"` entry to `$CODEX_HOME/config.toml`, even with
  `--ignore-user-config`. Before Task 7 the worker inherited the caller's `CODEX_HOME`, so the
  same write presumably went to the user's own `config.toml`; that was not tested. With the job
  home it stays in `<report_dir>/codex-home`. With `--ephemeral`, no `sessions/` directory is
  created.
- **A stray `apply_patch` function call is still possible.** It never happened in 13 runs with
  the shipped instructions. If it does, the attempt still finishes, and stderr shows
  `unsupported call: apply_patch`. Classify that as a model slip, not an environment failure.
- **`codex debug prompt-input` must not be the self-check for what the model sees.** It omits
  tools and, without lite, the base instructions. A loopback stub capture, as in `render`, is
  the reliable view.

## Wrapper smoke (Task 9)

Task 9 runs the worker, every verification command and every native step as
`sandbox-exec -f <report_dir>/permissions.sb <command>`, a profile generated per job from
`<state>/permissions.json`. The same pilot was run once on this Mac against the real oMLX
model, with a fresh pilot and a fresh state directory:

```sh
python3 scripts/prepare_pilot.py --root <root>
bin/local-worker run --task-file <root>/task.json --state-dir <root>/state --json
```

| Status | Tests | Worker s | Job wall s | Tokens | `permissions` | Primary checkout |
| --- | --- | ---: | ---: | --- | --- | --- |
| ok, exit 0 | 1 passed, `deny_list_applied: true` | 19.2 | 20.7 | 12,918 / 11,459 / 458 | `mode: full_host`, `deny_list_applied: true`, `reason: null`, `denials: []`, profile sha256 `278f293e…` (12 statements) | unchanged; `sentinel.txt` still dirty; `primary_changed_during_job: false` |

Model `Qwen3.5-9B-OptiQ-4bit`, launcher catalog `870e20c8…`. The worker ran `cat`, the
`apply_patch` heredoc (2 `file_change` items) and `python3 -m unittest -v`; its stderr was
empty and the stream had no error item. Codex wrote the attempt's `-o` file in place, so
re-opening that one file inside the shut state directory is enough (the last message reads
"Done. Fixed `add.py` …").

The same job's profile was then exercised directly
(`/usr/bin/sandbox-exec -f <report_dir>/permissions.sb …`):

| Command under the job's profile | Result |
| --- | --- |
| `touch <state>/permissions.json`, `echo x >> <state>/permissions.json` | `Operation not permitted` |
| `echo x >> <report_dir>/report.json`, `echo x >> <report_dir>/permissions.sb` | `Operation not permitted` |
| `mv <state> <state>-moved` | `Operation not permitted` |
| `touch <root>/repo/add.py` (primary checkout) | `Operation not permitted` |
| `echo x > <root>/repo/.git/hooks/post-checkout` | `Operation not permitted` |
| `ls ~/.ssh` | `Operation not permitted` |
| `touch <worktree>/.smoke-probe`, `touch <report_dir>/codex-home/.smoke-probe` | allowed (probes removed) |

`find_denials` over the output of `mv <root>/repo/add.py <root>/repo/b.py` and
`mv <state> <root>/x` under that profile returned exactly `<root>/repo/add.py`,
`<root>/repo/b.py` and `<state>` (all `write`).
