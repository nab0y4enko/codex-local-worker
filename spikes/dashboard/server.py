#!/usr/bin/env python3
"""Small, dependency-free MCP Apps server used only by the Task 27 desktop spike."""

import json
import os
import stat
import sys
from pathlib import Path


RESOURCE_URI = "ui://local-worker/dashboard"
MIME_TYPE = "text/html;profile=mcp-app"


def _load_dashboard():
    package_root = Path(__file__).resolve().parent
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixture_path = fixtures_dir / "dashboard.json"
    if fixtures_dir.is_symlink() or fixture_path.is_symlink():
        raise ValueError("dashboard fixture directory and file must not be symlinks")
    resolved_fixtures = fixtures_dir.resolve(strict=True)
    resolved_fixture = fixture_path.resolve(strict=True)
    if resolved_fixtures != package_root / "fixtures" or resolved_fixture.parent != resolved_fixtures:
        raise ValueError("dashboard fixture must stay inside the package fixtures directory")
    directory_descriptor = os.open(
        str(fixtures_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            raise ValueError("dashboard fixture directory must be a directory")
        descriptor = os.open("dashboard.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                             dir_fd=directory_descriptor)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("dashboard fixture must be a regular file")
            data = json.load(handle)
    finally:
        os.close(directory_descriptor)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("fixture must be an object with a rows list")
    return data


def _text_summary(data):
    rows = data.get("rows", [])
    lines = ["Demo dashboard revision %s" % data.get("revision", "unknown")]
    lines.extend("%s — %s" % (row.get("job", "(unnamed)"), row.get("status", "unknown"))
                 for row in rows if isinstance(row, dict))
    return "\n".join(lines)


def _tool_definition(ui_negotiated):
    tool = {
        "name": "open_dashboard",
        "description": "Open the synthetic, read-only Task 27 dashboard demo.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    if ui_negotiated:
        tool["_meta"] = {"ui": {"resourceUri": RESOURCE_URI}}
    return tool


def _read_widget():
    path = Path(__file__).with_name("widget.html")
    return path.read_text(encoding="utf-8")


class McpServer:
    def __init__(self):
        self.ui_negotiated = False

    def handle(self, message):
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            capabilities = message.get("params", {}).get("capabilities", {})
            extensions = capabilities.get("extensions", {}) if isinstance(capabilities, dict) else {}
            ui = extensions.get("io.modelcontextprotocol/ui", {}) if isinstance(extensions, dict) else {}
            self.ui_negotiated = MIME_TYPE in ui.get("mimeTypes", []) if isinstance(ui, dict) else False
            result = {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "local-worker-dashboard-spike", "version": "0.1.0"},
            }
            return self._success(request_id, result)
        if method == "ping":
            return self._success(request_id, {})
        if method == "tools/list":
            return self._success(request_id, {"tools": [_tool_definition(self.ui_negotiated)]})
        if method == "resources/list":
            return self._success(request_id, {"resources": [{
                "uri": RESOURCE_URI,
                "name": "Dashboard demo widget",
                "description": "Static synthetic dashboard view for the Task 27 MCP Apps spike.",
                "mimeType": MIME_TYPE,
            }]})
        if method == "resources/read":
            if message.get("params", {}).get("uri") != RESOURCE_URI:
                return self._error(request_id, -32602, "Unknown resource URI")
            return self._success(request_id, {"contents": [{
                "uri": RESOURCE_URI,
                "mimeType": MIME_TYPE,
                "text": _read_widget(),
                "_meta": {"ui": {"csp": {}, "prefersBorder": True}},
            }]})
        if method == "tools/call":
            params = message.get("params", {})
            if params.get("name") != "open_dashboard":
                return self._error(request_id, -32602, "Unknown tool")
            try:
                data = _load_dashboard()
            except (OSError, ValueError, json.JSONDecodeError) as error:
                return self._error(request_id, -32603, "Could not read demo fixture: %s" % error)
            result = {
                "content": [{"type": "text", "text": _text_summary(data)}],
                "structuredContent": data,
            }
            if self.ui_negotiated:
                # Keep result metadata as a compatibility probe; current MCP Apps links the
                # resource from tools/list metadata (above).
                result["_meta"] = {"ui": {"resourceUri": RESOURCE_URI}}
            return self._success(request_id, result)
        if request_id is not None:
            return self._error(request_id, -32601, "Method not found")
        return None

    @staticmethod
    def _success(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}


def main():
    server = McpServer()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                response = server._error(message.get("id") if isinstance(message, dict) else None,
                                         -32600, "Invalid JSON-RPC request")
            else:
                response = server.handle(message)
        except json.JSONDecodeError:
            response = server._error(None, -32700, "Parse error")
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
