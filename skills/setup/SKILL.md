---
name: setup
description: Use when installing, first configuring, or re-pointing the Local Worker plugin on a machine, including a new Codex home or a legacy hand-installed local-worker policy.
---

# Local Worker Setup

One-time, per-machine installation of this plugin's user-wide routing default. The model selected
in the Codex window keeps planning and reviewing; setup only makes every project route
implementation to the pinned local Qwen worker through the bundled launcher. Setup changes no
window model, no global provider, and no app-wide enforcement.

## Resolve the plugin root first

This file is `<root>/skills/setup/SKILL.md`, so the plugin root is two directories above it: the
directory whose `plugin.json` declares `"name": "codex-local-worker"`. Every command below uses that
resolved root; never assume a personal checkout path, because the installed root changes on
update or reinstall.

## Check, then apply

`src/plugin_setup.py` is stdlib-only Python and takes exactly these forms
(`$CODEX_HOME` is respected, default `~/.codex`):

```sh
python3 <root>/src/plugin_setup.py --check                      # read-only report
CODEX_HOME=/path/to/codex/home python3 <root>/src/plugin_setup.py --check
python3 <root>/src/plugin_setup.py --apply                      # authorized installation
```

Run `--check` first and read it. It reads only, writes nothing, and reports the resolved root, the
`CODEX_HOME` target, manifest/skill discovery, the state of the managed instruction block, the
launcher's presence, and the prerequisites below. Then run `--apply` only when the user authorized
installation; it must be idempotent, so a second `--apply` is a no-op.

### What `--check` reports

Output is one JSON object on stdout. `--check` creates no directory, no backup and no state file,
and running it writes no bytecode. Read:

- `plugin` — `name`, `version`, `root`, `manifest` and `onboarding_skill` for this install.
- `codex_home`, `state_dir` — the resolved `$CODEX_HOME` (default `~/.codex`) and
  `$LOCAL_WORKER_STATE_DIR` (default `~/.local-implement`).
- `package` — presence of the launcher `bin/local-worker` and its engine `src/local_worker.py`,
  the canonical policy, this helper, every skill and the manifest, plus `legacy`, the shipped
  migration input. A launcher without its engine is a malformed package (exit `3`), never readiness.
- `managed_block.state` — `absent` (nothing owned yet), `current` (points at this root), `drift`
  (points at an older install root), or `malformed` (unbalanced, duplicated or inline markers).
- `agents_override` — read-only inspection of `$CODEX_HOME/AGENTS.override.md`: `state` is
  `absent`, `empty` (present but whitespace-only, which Codex discovery ignores) or `active`
  (nonempty, so Codex reads the override before `AGENTS.md` and `supersedes_managed_block` is
  `true`) and a blocker: `--check` exits `7`, and `--apply` refuses before any backup or write. The
  file is read only to decide whether it is empty; the report carries the path and that state, and
  the override's contents are never printed or disclosed.
- `environment` — `python`, `git`, `codex_cli` (found on `PATH` or through
  `$LOCAL_WORKER_CODEX_BIN`), `omlx` (file presence only, contents never read), and `native`
  (`xcrun`, optional and needed only for native Apple jobs).
- `preflight` — `requested: false` and `endpoint_passed: false` unless `--preflight` was passed.

### What `--apply` changes

One small managed block inside `$CODEX_HOME/AGENTS.md`, delimited by the fixed comments
`<!-- local-worker begin -->` and `<!-- local-worker end -->`, pointing at the canonical policy shipped
with this plugin: `<root>/skills/local-worker/references/global-policy.md`. The tiny block is the
only global text the plugin owns; the authoritative policy text stays in the package, so an update
replaces it in place instead of growing the global file. Installed paths are embedded as Markdown
links wrapped in `<>`, so an install root containing spaces stays valid.

Everything outside the markers is preserved byte-for-byte. Existing bytes are copied to a
timestamped, read-only backup under `$LOCAL_WORKER_STATE_DIR/setup-backups/` before an atomic
replacement, so a repeated `--apply` that would change nothing writes nothing and makes no extra
backup. Malformed, duplicated, unbalanced or inline markers are refused with the file untouched, and
a missing or unreadable canonical policy is refused too. Setup never rewrites `config.toml`, auth,
provider settings, model weights, OS permissions or project files.

While `$CODEX_HOME/AGENTS.override.md` is nonempty - or cannot be inspected at all: unreadable, a
symlink, or not a regular file - it is an exit `7` blocker. `--check` reports it and exits nonzero;
`--apply` refuses before making any backup or touching `AGENTS.md`, because Codex reads that override
first and writing the block would claim an active default that does not exist. The override is never
modified or deleted, and an empty or whitespace-only override is compatible with a normal install.

The `apply` receipt records `changed`, `reason` (`installed`, `updated`, `migrated_legacy`, or the
no-op reason), `destination`, `backup` (`null` when there was nothing to back up),
`before_sha256`/`after_sha256`, `installed.*.sha256` for the shipped policy, skills, helper and
manifest, and `verified_state` re-read from disk after a change. Keep that receipt: it is the
evidence that unrelated instructions survived, because a later read-only `--check` cannot prove what
it never saw.

A legacy `$CODEX_HOME/AGENTS.md` that matches the migrated historical policy byte-for-byte is
reduced to a short heading plus the managed block instead of being kept as a duplicate; any other
unrelated instructions are kept exactly as they are. A legacy per-user skill copy such as
`~/.codex/skills/local-worker/SKILL.md` may be retired only after the plugin install is
qualified, and never automatically.

Setup starts no worker, runs no model inference, and edits no project code.

## Exit codes and the optional endpoint proof

`0` succeeded; `2` is a usage error (`--check` and `--apply` are mutually exclusive); `3` is a
malformed package or a missing canonical policy; `4` is a malformed managed block; `5` is a failed
apply; `6` is a failed explicit `--preflight`; `7` is a nonempty or uninspectable
`$CODEX_HOME/AGENTS.override.md`, which supersedes (or hides) the managed block.

`--preflight` (bounded by `--preflight-timeout`, default 60 seconds) is the only step that contacts
the model server, and it is delegated entirely to the packaged launcher
`<root>/bin/local-worker preflight` instead of issuing a request of its own. `endpoint_passed` is
`true` only when that launcher exits 0 inside the timeout with a complete success receipt: a JSON
object with nonempty `endpoint`, `canonical_model` and `api_model`, `model_fallback` exactly `false`,
and a `served_models` list that contains `api_model`. Any other output - no JSON, a JSON list or
string, a blocked or partial object, fallback enabled, or a model that is not actually served - is
reported as failed with the concrete reason in `preflight.reason`, never as an endpoint claim; the
launcher's own values are redacted rather than echoed, and `stdout_parsed` says whether JSON was
decoded at all. Routing policy stays inside the launcher: this checks the receipt it delegated, it
does not re-issue a request. Never report a passing endpoint without that field in hand.

## Prerequisites (reported, not installed)

- macOS with `python3` (stdlib only), `git`, and the Codex CLI on `PATH`.
- oMLX serving an OpenAI-compatible endpoint on `http://127.0.0.1:8000/v1` only, with
  `~/.omlx/settings.json` carrying `auth.api_key`, loopback `server.host`/`server.port` on `8000`,
  and `model.model_fallback: false`.
- `~/.omlx/model_settings.json` mapping `Qwen3.8-Flash-Next-UD-Q4_K_XL` to the alias
  `qwen-3.8-nf-splash`.
- Xcode and a booted simulator only for native Apple jobs; nothing in this plugin needs them to
  install.

Missing prerequisites are reported as concrete blockers. Setup does not install or upgrade oMLX,
Xcode, or the Codex CLI, does not repair account permissions, and never falls back to a cloud
model.

## No credentials

The plugin contains no credentials, and neither does the managed block. Never copy an API key,
token, or `~/.omlx/settings.json` value into plugin files, `AGENTS.md`, a skill, or a task
contract; the launcher reads what it needs at run time and redacts secrets from its reports.

## Verify afterwards

Run [doctor](../doctor/SKILL.md) — `python3 <root>/src/plugin_setup.py --check` — and then
`<root>/bin/local-worker preflight` for the actual endpoint. Report actual output; a plugin that
installs is not the same claim as a worker that ran.
