# Task 27 dashboard MCP Apps spike

**Status: prepared / desktop not run.** No GO or NO-GO decision is assigned. The actual desktop
rendering gate remains open pending explicit approval to add the temporary local marketplace and
reload the installed app if needed.

## What is prepared

- `spikes/dashboard/server.py` is a dependency-free stdio MCP server with `initialize`,
  `tools/list`, `tools/call`, `resources/list`, `resources/read`, and `ping`. The only tool is
  `open_dashboard`; fixture data is synthetic and read-only. It reads only the fixed
  `spikes/dashboard/fixtures/dashboard.json` file beside the server. The server does not accept a
  fixture path from the environment or tool arguments. It rejects a symlinked fixture directory
  or file, resolves the target under the package fixture directory, opens with `O_NOFOLLOW` where
  supported, and requires a regular file. Tests change revision A to B only in a copied disposable
  package.
- `spikes/dashboard/widget.html` is static HTML with a table, an embedded PNG data URI, an MCP Apps
  `ui/initialize` bridge handshake, a refresh button using `tools/call`, and a PiP request button
  shown only when the host advertises `pip`. Its visible diagnostics distinguish the app-declared
  display modes, the host-advertised modes, and the mode returned by a PiP request.
- UI metadata is added only when the peer advertises the `io.modelcontextprotocol/ui` extension
  with `text/html;profile=mcp-app`. The tool description carries the standard `_meta.ui.resourceUri`
  link; the tool result repeats that link as an additional compatibility probe and includes a
  useful text fallback. This follows the stable [MCP Apps specification](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx):
  resource linkage is in tool metadata, while extension support is capability-negotiated. The
  widget reports its declared `appCapabilities.availableDisplayModes` separately from the host's
  `hostContext.availableDisplayModes` and records the actual response to a `pip` request.
- `tests/test_dashboard_spike.py` exercises the JSONL stdio protocol with real subprocess pipes,
  capability negotiation and text-only fallback, resource URI/MIME, embedded PNG signature and
  CRCs, tool result data, fixture revision refresh, an ignored `../../outside.json` environment
  override, and rejected outside-target and outside-ancestor symlinks.

## Evidence collected

| Check | Result |
|---|---|
| Protocol and fixture-boundary tests | `python3 -m unittest tests.test_dashboard_spike` — 7 tests passed. |
| Python syntax | `python3 -m py_compile spikes/dashboard/server.py tests/test_dashboard_spike.py` — passed. |
| App package | `/Applications/ChatGPT.app` version `26.1021.31345`; prior local readiness inspection identifies it as the Codex variant. No app UI was opened, installed, reloaded, or restarted for this spike. |
| CLI package | `codex-cli 0.164.0-alpha.11.1`. Isolated `codex plugin marketplace add`, `codex plugin add`, `codex plugin list`, and `codex mcp list` succeeded with `HOME=CODEX_HOME=/tmp/dashboard-spike-cli-home-r1-20261025`. The CLI listed `dashboard_spike` as an enabled stdio MCP server with no environment overrides. |
| Actual Codex MCP handshake | In the same isolated home, `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server --listen stdio://` accepted a no-inference JSON-RPC `initialize` handshake, then `mcpServerStatus/list` started the plugin server and reported `toolsError: null` with `open_dashboard`. The request was repeated with and without the `io.modelcontextprotocol/ui` capability in the outer app-server initialize; the status response exposed the tool name but not `_meta` fields. This verifies MCP server startup and tool listing in the CLI app-server, not desktop rendering or UI capability support. No prompt, model inference, login, or real user config access was performed. |
| Desktop rendering / advertised host modes / PiP | Not observed; requires the approved desktop install/reload step. |
| A→B mounted refresh / unsolicited updates | Not observed in the host. The fixture and refresh round trip are prepared; the server has no push mechanism. |

The host gate must record what the desktop itself sends in `hostContext.availableDisplayModes`,
the initial `displayMode`, and the actual mode returned after any permitted PiP request. The UI's
declared modes are not host capability evidence. Likewise, an explicit refresh round trip is not
evidence of server push.

## Temporary marketplace package

Prepared outside the installed package cache and outside the repository:

```text
/tmp/codex-dashboard-mcpapps-spike-20261025/
  .agents/plugins/marketplace.json
  plugin/.codex-plugin/plugin.json
  plugin/.mcp.json
  plugin/server.py
  plugin/widget.html
  plugin/fixtures/dashboard.json
```

The plugin identity is `dashboard-mcpapps-spike-20261025`, distinct from `codex-local-worker`.
The CLI qualification changed only the disposable test home at
`/tmp/dashboard-spike-cli-home-r1-20261025`; the real `~/.codex` and desktop settings were not
read or changed. The marketplace package version is `0.1.1` and the MCP manifest contains no
external fixture path or environment override.

After the parent reviews this prepared result and obtains approval for the desktop action, the
CLI-managed marketplace command is:

```sh
codex plugin marketplace add /tmp/codex-dashboard-mcpapps-spike-20261025
```

Then install the uniquely named plugin in the Plugins Directory, or use:

```sh
codex plugin add dashboard-mcpapps-spike-20261025@dashboard-spike-20261025
```

The setting to record is only the `dashboard-spike-20261025` marketplace source plus the
`dashboard-mcpapps-spike-20261025` plugin install/enable choice. Before making those changes,
record `codex plugin marketplace list` and `codex plugin list --json` so rollback can restore the
prior state. Rollback uses the inverse commands:

```sh
codex plugin remove dashboard-mcpapps-spike-20261025@dashboard-spike-20261025
codex plugin marketplace remove dashboard-spike-20261025
```

If app reload is requested by the desktop, do it only as part of the approved gate and record
that action; after rollback, reload again only if needed to make the removal effective. The
temporary marketplace can then be removed with `rm -rf /tmp/codex-dashboard-mcpapps-spike-20261025`.
The disposable CLI qualification home can then be removed with
`rm -rf /tmp/dashboard-spike-cli-home-r1-20261025`.
The installed app remains untouched until that approval. Do not copy or inspect session,
message, credential, or authentication data for this gate.

## Decision

**Undecided.** Protocol and isolated CLI preparation pass. Desktop support, widget rendering,
host-advertised modes, PiP, and mounted refresh are unknown because the app was not modified or
reloaded. Do not start Task 28 from these results. Complete the desktop observations first; a
missing observation is not a failed host capability.
