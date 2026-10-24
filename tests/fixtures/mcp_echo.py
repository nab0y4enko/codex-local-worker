#!/usr/bin/env python3
"""Real stdlib MCP fixture: one ``echo`` tool over JSONL stdio, no network and no model.

A manifest points a stdio server at this file so the PREPROBE and a later real tool call exercise
Codex' own MCP client instead of a mock. It refuses to start unless ``TASK_TOOL_TOKEN`` is present
and non-empty, and nothing it writes can contain that value.
"""
from __future__ import annotations

import json
import os
import sys

TOKEN_ENV = "TASK_TOOL_TOKEN"
SERVER_INFO = {"name": "fixture", "version": "1"}
DEFAULT_PROTOCOL = "2025-06-18"
TOOL = {
    "name": "echo",
    "description": "Echo the text it is given, prefixed with 'fixture:'.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
}


def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(request_id, result):
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code, message):
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _call(request_id, params):
    name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if name != TOOL["name"]:
        _result(request_id, {"content": [{"type": "text", "text": "unknown tool"}],
                             "isError": True})
        return
    text = arguments.get("text", "") if isinstance(arguments, dict) else ""
    _result(request_id, {"content": [{"type": "text", "text": "fixture:" + str(text)}],
                         "isError": False})


def _handle(message):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if request_id is None:                       # a notification: acknowledged by silence
        return
    if method == "initialize":
        _result(request_id, {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                             "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    elif method == "tools/list":
        _result(request_id, {"tools": [TOOL], "nextCursor": None})
    elif method == "tools/call":
        _call(request_id, params)
    elif method == "ping":
        _result(request_id, {})
    elif request_id is not None:
        _error(request_id, -32601, "method not found: %s" % (method,))


def main():
    if not os.environ.get(TOKEN_ENV, "").strip():
        sys.stderr.write("mcp_echo: %s must be set to a non-empty value\n" % TOKEN_ENV)
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _error(None, -32700, "invalid JSON")
            continue
        if not isinstance(message, dict):
            _error(None, -32600, "expected a JSON-RPC object")
            continue
        try:
            _handle(message)
        except Exception as error:  # noqa: BLE001 - never echo a payload that could hold a secret
            if message.get("id") is not None:
                _error(message["id"], -32603, error.__class__.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
