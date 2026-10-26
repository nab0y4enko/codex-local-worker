# Exact saved OpenCode setup and current local profile

The successful October22 cloud-led comparison used **OpenCode2.1.2**, native OpenCode agents
configured by **native-opencode-agent-setup0.1.0**, from
`https://github.com/nab0y4enko/opencode-agent-setup`. The tracked benchmark receipt/profile was
introduced in commit`1eb5473`. This was not OMO or oh-my-opencode-slim. The setup wizard writes
native configuration; it does not patch OpenCode binaries.

The separate custom **agent-usage** plugin provides the usage sidebar, `/usage` panel and
completion reports. It has no independent package-version field; its identity is the source
checkout/commit, not an invented plugin semver. Its TUI reads native session data and its server
subscribes to completion events and reads SQLite, generating text/JSON reports without invoking
a model. The routing comes from native agent configuration, not from the usage plugin.

## Routing

| Role | Saved successful run / profile | Current global config |
|---|---|---|
| Default Orchestrator | openai/gpt-6.1-sol#medium; 8 steps | Same |
| General implementation/tests | omlx/qwen-3.8-nf-splash; 24 steps; no delegation | Same |
| Plan | openai/gpt-6.1-sol#high | openai/gpt-6.1-sol#medium |
| Build | openai/gpt-6.1-sol#high | Same |
| Optional independent reviewer | openai/gpt-6.1-sol#high | Same |
| Summary / compaction | openai/gpt-6.1-sol#medium | Same |
| Title | Local Qwen | Same |
| Explore | Not exercised by the saved successful run | Local Qwen; 12 steps |

The historical receipt pins the local API model alias, not a model-weights checksum; it does
not prove the exact historical quantization behind that alias.

Actual successful execution used only the medium Sol coordinator and one foreground local General
worker: three cloud turns and19 local turns,122.795 seconds. No extra Build or reviewer child was
used. It recorded6,493 cloud input,756 output and6,400 cache-read tokens; these provider
categories differ from Codex counters. Against its own saved cloud-only control, input fell47.9%
and output44.0%; it was1.99x slower. This is one small matched task, not a general guarantee.

## What is still installed

- Binary: `/opt/homebrew/bin/opencode`, currently **v2.1.4**, not the benchmark's2.1.2.
- Global config: `~/.config/opencode/opencode.json`; default_agent=`orchestrator`, warming=false.
- Local provider: `@opencode/ai/providers/openai-compatible`, baseURL
  `http://127.0.0.1:8000/v1`; Qwen model config context262144, output8192. Credential values
  were not displayed; private file/environment references are preserved in the sanitized copy.
- Every configured OpenAI role requests body.service_tier=`default` (Standard).
- Current setup checkout: `~/Projects/opencode-agent-setup`, commit
  `25d3757` (after the October23 bounded-routing revision `bc4a743`).
- Plugin link: `~/.config/opencode/plugins/agent-usage` points to that checkout's
  `plugin/agent-usage`; the target exists and `plugin status` confirms installation.
- Completion reports: `~/.local/state/opencode-agent-setup/usage/<session>.txt|.json`.

The current prompts include the later October23 bounded-routing changes. They are not byte-for-
byte the historical benchmark prompts. The saved `cloud-led-effective-agents.json` preserves the
actual earlier role definitions. Current role instructions, permissions and model limits are
preserved separately in a sanitized full config. Current2.1.4 CLI also differs from the old
documented2.1.2 startup flags; do not blindly reuse old commands or assume plugin/runtime
compatibility solely because the symlink exists. No model generation, configuration change,
login, restart, upgrade/downgrade or migration was performed to collect this evidence.

Recovery files (outside this repository):

- `~/.local/share/codex-local-worker/setup-report-overhead-20261026/opencode-current.sanitized.json`
- `~/.local/share/codex-local-worker/setup-report-overhead-20261026/opencode-benchmark-effective-agents.json`
- `~/.local/share/codex-local-worker/setup-report-overhead-20261026/opencode-provenance.json`

Source reports: `~/Projects/opencode-agent-setup/docs/cloud-led-comparison.md`,
`docs/balanced-profile.md`, `docs/benchmarks/2026-10-22-cloud-led.json`, and the plugin's
`server.js`, `tui.tsx` and `report.js`. Current OpenCode files remain unchanged.
