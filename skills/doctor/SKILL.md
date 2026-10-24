---
name: doctor
description: "Use when diagnosing or verifying an existing Local Worker plugin install: routing block, launcher presence, model endpoint, and native readiness, without writing anything."
---

# Local Worker Doctor

Read-only health check for an installed Local Worker plugin. Doctor reports; it never repairs,
installs, or rewrites configuration. Use [setup](../setup/SKILL.md) for any change.

Resolve the plugin root from this file's path: it is `<root>/skills/doctor/SKILL.md`, so the root is
two directories above it and is the directory containing `plugin.json`.

## Commands

```sh
python3 <root>/src/plugin_setup.py --check
CODEX_HOME=/path/to/codex/home python3 <root>/src/plugin_setup.py --check
python3 <root>/src/plugin_setup.py --check --preflight
```

`--check` is the default and touches nothing. It prints one JSON object reporting the resolved root,
the `CODEX_HOME` target (respected, default `~/.codex`) and state directory, manifest and skill
discovery, whether the managed instruction block in `$CODEX_HOME/AGENTS.md` is `absent`, `current`,
`drift` (it points at a different install root) or `malformed`, whether the canonical policy file and
launcher `bin/local-worker` and `src/local_worker.py` exist, and the environment: `python`, `git`,
the Codex CLI on `PATH` or through `$LOCAL_WORKER_CODEX_BIN`, oMLX file presence with contents
never read, and optional native tooling. A malformed block exits nonzero; drift is reported with exit
status `0` and named in `managed_block.reason`.

A nonempty `$CODEX_HOME/AGENTS.override.md` - or one that cannot be inspected at all - is an exit `7`
blocker reported in `agents_override`: Codex reads that override before `AGENTS.md`, so the managed
block is not an active default and setup refuses to write before it is resolved. Doctor reads the
override only to decide whether it is empty and never prints its contents.

Doctor cannot prove that the unrelated instructions around the block survived an update or a
reinstall: only the setup receipt records that, through its `before_sha256`/`after_sha256` and
`backup`. Report the block's state and the receipt you were given; never claim preservation you did
not observe.

`--preflight` is opt-in (bounded by `--preflight-timeout`, default 60 seconds) and makes the actual
model-endpoint request only by delegating to the packaged launcher:

```sh
<root>/bin/local-worker preflight
```

`endpoint_passed` is `true` only when that launcher exited 0 inside the timeout with a well-formed
success *receipt*: a JSON object with a nonempty `endpoint`, `profile.model.api_model` present,
`model_fallback` exactly `false`, and a positive integer `context_window`.
Arbitrary JSON is not a receipt - a JSON list or string, a blocked or partial object, fallback
enabled, or no discovered model - and neither is silence: any of those names the concrete
reason in `preflight.reason` and exits nonzero, so an absent, silent or unconvincing launcher is never
reported as a passing endpoint. When it does pass it confirms the discovered model, loopback
endpoint, context window, and disabled fallback. It is the only step that contacts the server, and it qualifies
model routing only — never the native toolchain, simulator, or dependency cache.

## Reporting

Quote actual exit status and actual output for each step, and name the failing check. A missing
model server, an unavailable simulator, or a missing dependency cache is a blocker to report, not
something to route around: never switch the window model or the global provider to something else,
never use a cloud worker, and never invent a passing check. Doctor writes no files, so a fix always
goes back through an authorized `--apply` in setup.
