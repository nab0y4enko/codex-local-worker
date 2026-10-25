# Task 27 dashboard MCP Apps spike

**Status: attempt 2 installed; manual desktop observation pending.** No GO or NO-GO decision is
assigned. Attempt 1's temporary install was exactly rolled back. The same verified v0.1.2 package
has since been restored and reinstalled for a parent-coordinated manual observation. It remains
installed. No restart or reload has been performed.

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
| Protocol and fixture-boundary tests | `python3 -m unittest tests.test_dashboard_spike` — 7 tests passed; the parent reran the suite with 7 passing in 0.141 s. Cloud tests remain synthetic only. |
| Python syntax | `python3 -m py_compile spikes/dashboard/server.py tests/test_dashboard_spike.py` — passed. |
| App package | `/Applications/ChatGPT.app` version `26.1021.31345`; prior local readiness inspection identifies it as the Codex variant. No app UI was opened or reloaded. |
| CLI package | `codex-cli 0.164.0-alpha.11.1`. Isolated qualification listed `dashboard_spike` as an enabled stdio MCP server. In the real Codex CLI environment, the temporary `dashboard_spike` entry appeared as enabled with `python3 server.py`, the expected temporary package directory, and auth status `Unsupported`. |
| Actual Codex MCP handshake | In the same isolated home, `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server --listen stdio://` accepted a no-inference JSON-RPC `initialize` handshake advertising `io.modelcontextprotocol/ui`; `mcpServerStatus/list` started the plugin server and returned `toolsError: null` with `open_dashboard`. The status response did not expose `_meta` fields. This verifies MCP server startup and tool listing in the CLI app-server, not desktop rendering or UI capability support. No prompt, model inference, login, or real user config access was performed. |
| Attempt 1 install and rollback | Added `dashboard-spike-20261025` from `/tmp/codex-dashboard-mcpapps-spike-20261025` and installed `dashboard-mcpapps-spike-20261025@dashboard-spike-20261025` v0.1.2. Removed both with the inverse CLI commands. Post-rollback marketplace and plugin registries matched the saved baseline exactly; `dashboard_spike` and its installed cache were absent. The source and synthetic plugin cache were archived and verified externally before removing the temporary paths. |
| Attempt 2 current install | Restored the same v0.1.2 package from the verified archive. The server, widget, and fixture hashes still match the repository files byte-for-byte. Captured fresh pre-install registries, then re-added only the same marketplace and plugin. The post-install diff contains only those entries; `codex mcp list` shows `dashboard_spike` enabled with `python3 server.py` and auth status `Unsupported`. It remains installed pending manual observation. |
| Actual desktop rendering / advertised host modes / PiP | No observation yet. Earlier native computer-use access was rejected by the client with an explicit safety denial for `com.openai.codex`; AppleScript reported `SystemEvents` UI elements disabled. No permissions were changed and no programmatic UI route was used. The user is being asked to open the supported candidate link manually. |
| Runtime tool exposure | This task runtime's `ALL_TOOLS` did not contain `dashboard_spike` or `open_dashboard`; `codex mcp list` did show the installed server while the temporary plugin was present. Static registry/API inspection does not establish that the desktop would expose or render it. |
| A→B mounted refresh / unsolicited updates | Not observed in the host. The fixture and refresh round trip are prepared; the server has no push mechanism. |

Read-only inspection of the bundled app found a parser for `codex://plugins/{pluginId}/app/{toolName}`
and `codex://settings/{pluginId}/{toolName}` deep links. The candidate global entrypoint is
`codex://plugins/dashboard-mcpapps-spike-20261025%40dashboard-spike-20261025/app/open_dashboard`.
The agent has not invoked it; the parent has asked the user to open it manually and report the
widget contents/image/error and current/host display modes. Those observations are pending.

The host gate must record what the desktop itself sends in `hostContext.availableDisplayModes`,
the initial `displayMode`, and the actual mode returned after any permitted PiP request. The UI's
declared modes are not host capability evidence. Likewise, an explicit refresh round trip is not
evidence of server push.

## Temporary marketplace package

Originally prepared outside the installed package cache and repository. After attempt 1 rollback,
the exact source package was archived and verified externally, then removed. For attempt 2 it was
restored from that verified archive and is currently installed pending manual observation:

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
The marketplace package version is `0.1.2` and the MCP manifest contains no external fixture path
or environment override. Before the real CLI installation, only the marketplace list and plugin
registry were recorded. No user config, session, message, credential, or authentication data was
read or copied.

The approved installation used the CLI-managed marketplace command:

```sh
codex plugin marketplace add /tmp/codex-dashboard-mcpapps-spike-20261025
```

Then the uniquely named plugin was installed with:

```sh
codex plugin add dashboard-mcpapps-spike-20261025@dashboard-spike-20261025
```

The only persistent choices were the `dashboard-spike-20261025` marketplace source and the
`dashboard-mcpapps-spike-20261025` plugin install/enable state. Before installation, the exact
marketplace and plugin registries were captured. Rollback used the inverse commands:

```sh
codex plugin remove dashboard-mcpapps-spike-20261025@dashboard-spike-20261025
codex plugin marketplace remove dashboard-spike-20261025
```

Attempt 1's registry rollback was verified by exact pre/post comparison. The disposable CLI
qualification home also contained SQLite state and a config file, so only the synthetic plugin
cache was included in the verified archive; no such database or config file was read or copied.
Attempt 1's temporary directories were removed. Attempt 2 recreated only the prepared marketplace
source and installed plugin cache from the archive; the current plugin/marketplace registry delta
is limited to the spike. The app was not restarted or reloaded. Restart is authorized if needed for
the manual observation, but has not been performed. Cloud tests remain synthetic only.

## Decision

**Undecided.** Protocol and isolated CLI preparation pass. Attempt 1 install and rollback passed;
attempt 2 is installed and awaiting the user's manual observation. Desktop support, widget
rendering, host-advertised modes, PiP, and mounted refresh remain unknown. Do not start Task 28 from
these results. A missing observation is not a failed host capability.
