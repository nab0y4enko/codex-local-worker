---
name: doctor
description: Use when checking Local Worker installation, Codex compatibility, local model discovery, permissions, or simulator readiness without changing files.
---

# Local Worker Doctor

Run `<root>/bin/local-worker doctor --json` to collect a read-only readiness snapshot. Resolve
`<root>` from this file: it is `<root>/skills/doctor/SKILL.md`, so the root is two directories
above it. Doctor does not create state directories, change `permissions.json`, start a worker,
or make a model turn.

```sh
<root>/bin/local-worker doctor --json
<root>/bin/local-worker doctor --state-dir /path/to/state --settings /path/to/omlx/settings.json
```

The JSON object contains:

- `codex.version` and `codex.flags_ok`: the installed CLI version and whether required worker
  flags are available.
- `omlx`: the live discovery profile when the loopback model endpoint responds, otherwise an
  `error_code` to classify the discovery failure.
- `permissions.mode`, `permissions.deny_list`, and `permissions.wrapper_available`: the effective
  mode and read/write deny lists, plus whether the platform wrapper is available. Missing
  `permissions.json` is reported from in-memory defaults; no file is created.
- `simulators`: currently available simulators with UDID, name, platform, and state. An unavailable
  simulator toolchain produces an empty list.
- `state_dir` and `plugin_version`: resolved paths/version from this package's manifest.

Doctor runs `codex --version`, `codex exec --help`, local endpoint discovery, and a simulator
metadata listing. It does not run `codex exec`, change configuration, install dependencies, or
qualify a build or test. A reported simulator is available metadata, not proof that native tests
will pass; the native preflight plan must build, install, launch, and execute a positive test.

Treat missing Codex flags, discovery `error_code`, invalid permission configuration, and an empty
simulator list as findings to report. Do not guess a model or endpoint. Do not edit
`permissions.json`; use the launcher's explicit `permissions show`, `deny-read`, `allow-read`,
`deny-write`, or `allow-write` commands only when the user asks for that permission change.
For package migration, see [setup](../setup/SKILL.md). For job outcomes and recovery, see the
[local worker workflow](../local-worker/SKILL.md).
