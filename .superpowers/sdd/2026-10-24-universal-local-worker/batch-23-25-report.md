# Tasks 23–25 implementation report

Branch: `universal-local-worker`  
Starting HEAD: `bd94a1b`  
Final implementation HEAD: `b9484c4`

## Task 23 — first-party package and branding

Moved the plugin manifest to `.codex-plugin/plugin.json`, added original geometric PNG assets,
made the manifest the only plugin version source, and added the archive-based Codex install
check. Metadata was corrected to verified repository ownership (`Naboy4enko`,
`github.com/nab0y4enko/codex-local-worker`) and marked `UNLICENSED`; the package version is
`0.3.0`. The summary screenshot now shows a clearly labelled illustrative job dashboard rather
than an enlarged logo.

TDD evidence: initial package tests failed because the first-party manifest and
`plugin_setup.plugin_version` interface were absent. The package/setup tests passed after the
implementation (47 tests at that stage). `python3 scripts/check_plugin_install.py` passed on
the initial implementation commit and again on final committed HEAD. Both runs archive HEAD,
install to a disposable `HOME`/`CODEX_HOME`, and check skill discovery. On final HEAD, a separate
disposable-home install followed by `codex plugin list --json` showed
`codex-local-worker@codex-local-worker`, version `0.3.0`, installed and enabled.

## Task 24 — preserve the user's completion rule

Replaced the policy-owning global block with an optional two-line plugin pointer. Migration
preserves the exact user completion-rule bytes, avoids duplicate copies, retains backup and
atomic-write behavior, and leaves a plain unmarked `AGENTS.md` alone unless pointer installation
is explicitly requested. The removed policy source is not shipped in the plugin.

TDD evidence: initial migration tests failed because `USER_COMPLETION_RULE` and `migrate` were
missing. After implementation, `tests.test_plugin_setup` and `tests.test_legacy_migration`
passed (67 tests with the package tests). The shipped rule hash is
`ee423c25bfdaeb041b80a703ff434caf7ba210c01a5020794e79e34e33acc516`.

## Task 25 — doctor and workflow documentation

Added read-only `doctor` reporting Codex compatibility, live local-model discovery or an error
code, permission mode and deny lists, wrapper availability, simulator metadata, state directory,
and manifest version. Rewrote the local worker/setup/doctor skills and README for the dynamic
planner loop, read/write/capture jobs, review, escalation, summary, recovery, and permission
ownership. Setup docs include install instructions, migration behavior, and the explicit
user-owned commit/push/worktree-cleanup rule. Local retries are bounded per failure code and
within the original deadline; the escalation order and authorization guard are documented.

TDD evidence: initial doctor and package-anchor tests failed because `doctor_report` and the
workflow guidance were absent. Added four doctor tests and package documentation anchors. Final
focused verification after documentation refinements:

```text
python3 -m unittest tests.test_plugin_package tests.test_doctor tests.test_plugin_setup \
  tests.test_legacy_migration tests.test_ledger tests.test_notify
Ran 85 tests in 11.459s
OK
```

The final documentation and doctor changes were also checked with `git diff --check`. The two
existing summary CLI tests now capture their expected stdout so full-suite output does not dump
sample summary files and temporary paths. The notification negative-path test captures its two
expected warnings with `assertLogs`.

## Full suite and install verification

Full-suite run (managed command session `52553`) completed before the final documentation-only
wording refinements and test stdout capture:

```text
Ran 960 tests in 135.916s
OK
```

No test failures occurred. At that point, the notification negative-path test printed two expected
`macOS notification failed` messages and passed. The focused test subsequently confirmed both
warnings through `assertLogs` without emitting them. The retained full-suite output is
[`batch-23-25-fullsuite.log`](batch-23-25-fullsuite.log).

Final committed-HEAD install check:

```text
python3 scripts/check_plugin_install.py
ok
```

The script uses `git archive HEAD`, a temporary `HOME` and `CODEX_HOME`, checks the archived
manifest version against `codex plugin list --json`, and verifies the three installed skills are
visible to `codex debug prompt-input`. It passed on final HEAD `b9484c4`; the installed version
matched `0.3.0`. No real `~/.codex` state was changed and no model inference or cloud call was made.

## Commits

- `94ca0a9 feat: adopt the first-party plugin layout with branding`
- `76371f3 fix: correct plugin metadata and package version`
- `a202b09 feat: keep the user's completion rule outside the plugin`
- `cbd2952 docs: rewrite the skills and README for the universal worker`
- `b9484c4 test: assert installed version and capture notifier warnings`

No push or merge was performed.
