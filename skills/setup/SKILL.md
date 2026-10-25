---
name: setup
description: Use when installing or diagnosing the Local Worker plugin package, or when migrating its old managed AGENTS.md block.
---

# Local Worker Setup

Setup validates the installed plugin and can migrate an older managed block in
`$CODEX_HOME/AGENTS.md`. It does not install a model, change Codex permissions, edit provider
settings, or route every project automatically.

Install the plugin with the Codex CLI by following the repository's
[install steps](../../README.md#install): choose the local checkout command for the version in
this checkout, or the GitHub marketplace command for the published default branch; then install
`codex-local-worker@codex-local-worker` and run the installed doctor's read-only report.

Resolve the plugin root from this skill: it is `<root>/skills/setup/SKILL.md`, two directories
below `<root>`. The first-party manifest is `<root>/.codex-plugin/plugin.json`.

## Inspect first

```sh
python3 <root>/src/plugin_setup.py --check
CODEX_HOME=/path/to/codex/home python3 <root>/src/plugin_setup.py --check
python3 <root>/src/plugin_setup.py --check --pointer
```

`--check` is read-only. The JSON report includes plugin version, package contents, the resolved
`CODEX_HOME` and state directory, environment availability, override status, and
`migration.would_change` plus `migration.reason`. `--pointer` previews the optional pointer too.
The report never reads or prints oMLX settings or credentials. No credentials or settings contents
are exposed. Use
`<root>/bin/local-worker doctor --json` for live Codex, model endpoint, permission, and simulator
readiness.

## Apply an authorized migration

```sh
python3 <root>/src/plugin_setup.py --apply
python3 <root>/src/plugin_setup.py --apply --pointer
```

The default apply migrates an old managed block or an exact historical global policy into the
user's completion rule, byte-for-byte, outside plugin markers. It does not add a pointer to a new
or otherwise unmarked file. `--pointer` opts into a two-line marker block containing the installed
skill path and read-only `doctor` command. The pointer is removable with a later
`--apply` without `--pointer`; the user's completion rule remains outside the markers.

The **user-owned commit/push/worktree-cleanup rule** remains outside the plugin markers. Migration
keeps its exact bytes, does not duplicate a copy already present outside the markers, and does not
replace unrelated instructions with plugin policy text.

Before changing an existing file, setup stores a timestamped backup under
`$LOCAL_WORKER_STATE_DIR/setup-backups/` and replaces the file atomically. Text outside the old
markers is preserved byte-for-byte. If the completion rule already appears outside the markers,
setup does not duplicate it. The operation is idempotent: a second apply with the same options is
a no-op. Review the returned
`backup`, `before_sha256`, `after_sha256`, `reason`, and `verified_state` before relying on the
migration.

Setup refuses malformed, duplicated, unbalanced, or inline markers. A nonempty or unreadable
`$CODEX_HOME/AGENTS.override.md` is also a blocker because Codex reads the override first; setup
will not edit it or write `AGENTS.md` around it. The override contents are never shown. Exit
codes: `0` success, `2` usage, `3` invalid package, `4` malformed markers, `5` failed apply,
`6` requested endpoint check failed, `7` active override.

## Optional live endpoint check

```sh
python3 <root>/src/plugin_setup.py --check --preflight
```

This delegates a bounded live endpoint check to `<root>/bin/local-worker preflight`. It checks
Codex compatibility and current oMLX discovery; it does not run a worker or qualify native
tooling. Use [doctor](../doctor/SKILL.md) for the full read-only health report.
