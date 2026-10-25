"""Bounded required-tool PREPROBE through Codex' own MCP client, with no inference.

The probe asks the installed app-server to initialize and to report MCP server status only.
It never sends ``thread/start`` or ``turn/start`` and never calls a tool, so a missing or
broken required tool becomes a refusal before a single token is spent. Only safe metadata is
ever reported: raw responses, raw stderr and upstream error text (which can quote a credential)
are dropped on the floor.
"""
from __future__ import annotations

import contextlib
import json
import os
import selectors
import subprocess
import time
from pathlib import Path

import worker_observability as observability
import worker_tools as tools

__all__ = ["ToolProbeError", "probe_tools"]

CLIENT_NAME = "local_worker_tool_probe"
CLIENT_VERSION = "1"
LIST_METHOD = "mcpServerStatus/list"
PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DISCARDED_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 65536
PROBE_SLACK_SECONDS = 10
MIN_PROBE_SECONDS = 1.0
MODEL_KEY_ENV = "LOCAL_OMLX_API_KEY"


class ToolProbeError(ValueError):
    """A required MCP tool could not be confirmed before inference.

    The message names the server and the reason only. It never carries a raw response, a raw
    stderr line or upstream error text, because any of those could echo a selected credential.
    """


def probe_tools(loaded, *, codex_bin, env, overrides, deadline, codex_home):
    """Return a copy of ``loaded['evidence']`` with every selected server marked probed.

    ``overrides`` is the launcher's own ``config_overrides(profile, context_window, catalog)``
    (local pins, features off) and ``loaded['overrides']`` adds the selected MCP servers, so the
    probe asks the real Codex MCP client the very same questions the worker will run under.
    ``codex_home`` is the job's own Codex home, the one the worker gets too. No servers means
    nothing to check and the evidence comes back unchanged.
    """
    evidence = _evidence(loaded)
    servers = evidence.get("servers")
    if not isinstance(servers, dict) or not servers:
        return evidence
    names = sorted(str(name) for name in servers)
    for name in names:
        _require_enabled_tools(servers, name)

    budget = _budget(deadline, servers, names)
    if not str(codex_bin or "").strip():
        raise ToolProbeError("no Codex executable was available to probe the required MCP tools")
    home = Path(codex_home)
    if not home.is_dir():
        raise ToolProbeError("the job Codex home for the tool probe does not exist")
    process = _spawn(codex_bin, overrides, loaded, env, str(home))
    try:
        discovered = _Session(process, budget).exchange(names)
    finally:
        _discard(process)
    for name in names:
        meta = servers[name]
        found = discovered.get(name)
        if found is None:
            raise ToolProbeError("mcp server '%s' was not probed" % name)
        missing = [item for item in meta["enabled_tools"] if item not in found]
        if missing:
            raise ToolProbeError("mcp server '%s' did not advertise required tool(s): %s"
                                 % (name, ", ".join(missing)))
        meta["probed"] = True
        meta["discovered_tools"] = found
        meta["observed"] = False
    return evidence


def _evidence(loaded):
    """Deep-copy the loader evidence so a probe can never mutate the loader's own result."""
    if not isinstance(loaded, dict) or not isinstance(loaded.get("evidence"), dict):
        raise ToolProbeError("the loaded tool configuration carries no evidence to probe against")
    try:
        copied = json.loads(json.dumps(loaded["evidence"]))
    except (TypeError, ValueError):
        raise ToolProbeError("the loaded tool evidence is not JSON-safe") from None
    if not isinstance(copied, dict):
        raise ToolProbeError("the loaded tool evidence must be a JSON object")
    return copied


def _require_enabled_tools(servers, name):
    meta = servers[name]
    if not isinstance(meta, dict):
        raise ToolProbeError("mcp server '%s' evidence must be a map" % name)
    enabled = meta.get("enabled_tools")
    if not isinstance(enabled, list) or not enabled \
            or any(not isinstance(item, str) or not item.strip() for item in enabled):
        raise ToolProbeError("mcp server '%s' has no explicit enabled_tools to probe" % name)


def _budget(deadline, servers, names):
    """Slacken every server startup timeout by nothing, cap by the shared job budget."""
    total = 0
    for name in names:
        value = servers[name].get("startup_timeout_sec", tools.DEFAULT_STARTUP_TIMEOUT_SEC)
        total += value if isinstance(value, int) and not isinstance(value, bool) \
            else tools.DEFAULT_STARTUP_TIMEOUT_SEC
    total += PROBE_SLACK_SECONDS
    left = float(deadline.remaining()) if deadline is not None else float(total)
    budget = min(left, float(total))
    if budget < MIN_PROBE_SECONDS:
        raise ToolProbeError("the job budget has only %.1fs left, too little to probe the "
                             "required MCP tools before inference" % budget)
    return budget


def _spawn(codex_bin, overrides, loaded, env, home):
    """Start the app-server in its own process group, with the job's CODEX_HOME as home and cwd.

    The user's Codex home is never used: ``home`` is the job's own, the same one the worker runs
    in. The model credential is removed because a metadata probe must not authenticate.
    """
    argv = [str(codex_bin), "app-server", "--listen", "stdio://"]
    argv.extend(str(item) for item in (overrides or []))
    argv.extend(["-c", "mcp_servers={}"])
    argv.extend(str(item) for item in (loaded.get("overrides") or []))
    probe_env = dict(env) if isinstance(env, dict) else dict(os.environ)
    probe_env.pop(MODEL_KEY_ENV, None)
    probe_env["CODEX_HOME"] = home
    try:
        return subprocess.Popen(argv, cwd=home, env=probe_env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        raise ToolProbeError("the Codex app-server could not be started for the tool probe (%s)"
                             % (error.strerror or error.__class__.__name__,)) from None


def _discard(process):
    """Terminate the whole probe group and close every pipe, on success as well as on failure."""
    with contextlib.suppress(Exception):
        observability._terminate(process)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    with contextlib.suppress(Exception):
        process.wait(timeout=1)


class _Session:
    """One JSONL conversation with the app-server, bounded in time, per line and in total."""

    def __init__(self, process, budget):
        self.process = process
        self.end = time.monotonic() + budget
        self.buffer = b""
        self.next_id = 2
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        self.stderr_discarded = 0
        self.stdout_total = 0
        self.stdin_fd = process.stdin.fileno()
        try:
            os.set_blocking(self.stdin_fd, False)
        except OSError:
            raise ToolProbeError("the tool probe could not make the app-server input pipe "
                                 "nonblocking") from None
        self.write_selector = None

    def exchange(self, names):
        """initialize, initialized, then one bounded status page walk per selected server."""
        try:
            self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"clientInfo": {"name": CLIENT_NAME,
                                                  "version": CLIENT_VERSION}}})
            self._await(1, "the app-server initialize handshake")
            self._send({"jsonrpc": "2.0", "method": "initialized"})
            return {name: self._server_tools(name) for name in names}
        finally:
            self.selector.close()
            if self.write_selector is not None:
                self.write_selector.close()

    def _left(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise ToolProbeError("the tool probe reached its deadline before the required MCP "
                                 "tools were confirmed")
        return left

    def _send(self, message):
        """Write one JSONL frame straight to the fd, so a stalled reader cannot wedge the probe.

        A page cursor comes back from the app-server and is echoed verbatim in the next request,
        so a single frame can be far larger than the pipe capacity. stdin is nonblocking and every
        retry re-checks the deadline, so the worst case is the remaining probe budget and then a
        clean refusal; nothing is ever buffered in a ``BufferedWriter`` waiting for a flush.
        """
        pending = memoryview((json.dumps(message) + "\n").encode("utf-8"))
        while True:
            left = self._left()
            try:
                written = os.write(self.stdin_fd, pending)
            except BlockingIOError:
                written = 0
            except OSError:
                raise ToolProbeError("the Codex app-server closed the probe pipe before the "
                                     "required MCP tools were confirmed") from None
            pending = pending[written:]
            if not pending:
                return
            self._await_writable(left)

    def _await_writable(self, timeout):
        """Wait for stdin to accept more bytes, bounded by the shared probe deadline."""
        if self.write_selector is None:
            self.write_selector = selectors.DefaultSelector()
            self.write_selector.register(self.stdin_fd, selectors.EVENT_WRITE, "stdin")
        try:
            self.write_selector.select(timeout)
        except OSError:
            raise ToolProbeError("the Codex app-server closed the probe pipe before the required "
                                 "MCP tools were confirmed") from None

    def _await(self, request_id, label):
        while True:
            message = self._message(label)
            if "method" in message:
                continue                      # a notification or server request: never answered
            if message.get("id") != request_id:
                raise ToolProbeError("%s returned a response for an unexpected request" % label)
            if "error" in message:
                raise ToolProbeError("%s was refused by the Codex app-server" % label)
            result = message.get("result")
            if not isinstance(result, dict):
                raise ToolProbeError("%s returned a result that is not a map" % label)
            return result

    def _message(self, label):
        while True:
            left = self._left()          # a notification flood must not outrun the deadline
            index = self.buffer.find(b"\n")
            if index != -1:
                line, self.buffer = self.buffer[:index], self.buffer[index + 1:]
                return self._decode(line, label)
            if len(self.buffer) > MAX_LINE_BYTES:
                raise ToolProbeError("%s produced a response line over %d bytes"
                                     % (label, MAX_LINE_BYTES))
            state = self._drain(left, label)
            if state == "closed":
                raise ToolProbeError("the Codex app-server ended the probe session unexpectedly "
                                     "while waiting for %s" % label)

    def _drain(self, timeout, label):
        """Read whichever pipe is ready; stderr is counted and dropped, never kept for reporting."""
        try:
            events = self.selector.select(timeout)
        except OSError:
            raise ToolProbeError("the tool probe could not read from the Codex app-server") from None
        if not events:
            raise ToolProbeError("the Codex app-server did not answer while waiting for %s"
                                 % label)
        for key, _ in events:
            try:
                chunk = os.read(key.fd, READ_CHUNK_BYTES)
            except OSError:
                chunk = b""
            if key.data == "stderr":
                self.stderr_discarded += len(chunk)
                if self.stderr_discarded > MAX_DISCARDED_BYTES:
                    self.stderr_discarded = MAX_DISCARDED_BYTES
                if not chunk:
                    with contextlib.suppress(KeyError):
                        self.selector.unregister(key.fileobj)
                continue
            if not chunk:
                return "closed"
            self.buffer += chunk
            self.stdout_total += len(chunk)
            if self.stdout_total > MAX_TOTAL_BYTES:
                raise ToolProbeError("the Codex app-server streamed over %d bytes of probe output"
                                     % MAX_TOTAL_BYTES)
            if len(self.buffer) > MAX_LINE_BYTES:
                raise ToolProbeError("%s produced a response line over %d bytes"
                                     % (label, MAX_LINE_BYTES))
        return "data"

    def _decode(self, line, label):
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            raise ToolProbeError("%s produced a response that is not JSON" % label) from None
        if not isinstance(message, dict):
            raise ToolProbeError("%s produced a response that is not a JSON-RPC object" % label)
        return message

    def _server_tools(self, name):
        """Walk ``mcpServerStatus/list`` pages for one server, bounded to ``MAX_PAGES``."""
        label = "the tool status of mcp server '%s'" % name
        found = []
        cursor = None
        seen = set()
        for _ in range(MAX_PAGES):
            params = {"serverName": name, "detail": "toolsAndAuthOnly", "limit": PAGE_LIMIT}
            if cursor is not None:
                params["cursor"] = cursor
            request_id = self.next_id
            self.next_id += 1
            self._send({"jsonrpc": "2.0", "id": request_id, "method": LIST_METHOD,
                        "params": params})
            result = self._await(request_id, label)
            entries = result.get("data")
            if not isinstance(entries, list):
                raise ToolProbeError("the Codex app-server reported no status list for mcp "
                                     "server '%s'" % name)
            entry = next((item for item in entries
                          if isinstance(item, dict) and item.get("name") == name), None)
            if entry is None:
                raise ToolProbeError("the Codex app-server did not report mcp server '%s'" % name)
            if entry.get("toolsError"):
                raise ToolProbeError("mcp server '%s' could not list its tools; the MCP client "
                                     "reported an error starting or querying it" % name)
            for tool in _tool_values(entry.get("tools"), name):
                tool_name = tool.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    raise ToolProbeError("the Codex app-server reported a tool of mcp server '%s' "
                                         "without a name" % name)
                if tool_name not in found:
                    found.append(tool_name)
            cursor = result.get("nextCursor")
            if cursor is None:
                return sorted(found)
            if not isinstance(cursor, str) or cursor in seen:
                raise ToolProbeError("the tool status page cursor for mcp server '%s' is invalid "
                                     "or repeats" % name)
            seen.add(cursor)
        raise ToolProbeError("mcp server '%s' reported more than %d pages of tools"
                             % (name, MAX_PAGES))



def _tool_values(raw, name):
    """Codex reports tools as a map keyed by tool name; a plain list is tolerated."""
    if raw is None:
        return []
    values = list(raw.values()) if isinstance(raw, dict) else raw
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise ToolProbeError("the Codex app-server reported a malformed tool list for mcp "
                             "server '%s'" % name)
    return values
