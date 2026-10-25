import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "spikes" / "dashboard" / "server.py"
RESOURCE_URI = "ui://local-worker/dashboard"


class DashboardSpikeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        fixture = Path(self.temp_dir.name) / "fixture.json"
        fixture.write_text(json.dumps({"revision": "A", "rows": [{"job": "demo-A", "status": "ready"}]}))
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, DASHBOARD_FIXTURE=str(fixture)),
        )
        self.fixture = fixture
        self.request_id = 0
        initialized = self.call("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {
                "mimeTypes": ["text/html;profile=mcp-app"]
            }}},
            "clientInfo": {"name": "dashboard-spike-test", "version": "1"},
        })
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        self.notify("notifications/initialized")

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()
        self.temp_dir.cleanup()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params=None):
        self.request_id += 1
        request_id = self.request_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                stderr = self.process.stderr.read()
                self.fail("server exited before replying: " + stderr)
            response = json.loads(line)
            if response.get("id") == request_id:
                if "error" in response:
                    self.fail("server error: " + json.dumps(response["error"]))
                return response["result"]

    def test_tool_advertises_widget_and_supplies_text_fallback(self):
        tool = self.call("tools/list")["tools"][0]
        self.assertEqual(tool["name"], "open_dashboard")
        self.assertEqual(tool["_meta"]["ui"]["resourceUri"], RESOURCE_URI)

        result = self.call("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertEqual(result["structuredContent"]["revision"], "A")
        self.assertIn("demo-A", result["content"][0]["text"])

    def test_client_without_ui_extension_gets_plain_mcp_tool(self):
        self.call("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "text-only-client", "version": "1"},
        })
        tool = self.call("tools/list")["tools"][0]
        self.assertNotIn("_meta", tool)
        result = self.call("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertNotIn("_meta", result)
        self.assertIn("demo-A", result["content"][0]["text"])

    def test_resource_has_mcp_apps_mime_and_embedded_png(self):
        resource = self.call("resources/read", {"uri": RESOURCE_URI})["contents"][0]
        self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/initialize", resource["text"])
        png_prefix = "data:image/png;base64,"
        start = resource["text"].index(png_prefix) + len(png_prefix)
        encoded = resource["text"][start:].split('"', 1)[0]
        png = base64.b64decode(encoded)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        chunk_types = []
        while offset < len(png):
            size = struct.unpack(">I", png[offset:offset + 4])[0]
            kind = png[offset + 4:offset + 8]
            content = png[offset + 8:offset + 8 + size]
            checksum = struct.unpack(">I", png[offset + 8 + size:offset + 12 + size])[0]
            self.assertEqual(zlib.crc32(kind + content) & 0xffffffff, checksum)
            chunk_types.append(kind)
            offset += size + 12
        self.assertEqual(chunk_types[-1], b"IEND")

    def test_fixture_revision_is_reflected_on_each_tool_call(self):
        self.fixture.write_text(json.dumps({"revision": "B", "rows": [{"job": "demo-B", "status": "done"}]}))
        result = self.call("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertEqual(result["structuredContent"]["revision"], "B")
        self.assertIn("demo-B", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
