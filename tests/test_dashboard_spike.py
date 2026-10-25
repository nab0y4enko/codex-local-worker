import base64
import json
import os
import shutil
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
        self.scratch = Path(self.temp_dir.name)
        self.package = self.scratch / "package"
        shutil.copytree(SERVER.parent, self.package)
        self.fixture = self.package / "fixtures" / "dashboard.json"
        self.outside_fixture = self.scratch / "outside.json"
        self.outside_fixture.write_text(json.dumps({
            "revision": "OUTSIDE", "rows": [{"job": "outside", "status": "secret"}]
        }))
        self.process = subprocess.Popen(
            [sys.executable, str(self.package / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, DASHBOARD_FIXTURE="../../outside.json"),
        )
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
        response = self.exchange(method, params)
        if "error" in response:
            self.fail("server error: " + json.dumps(response["error"]))
        return response["result"]

    def call_error(self, method, params=None):
        response = self.exchange(method, params)
        if "error" not in response:
            self.fail("expected server error, got: " + json.dumps(response))
        return response["error"]

    def exchange(self, method, params=None):
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
                return response

    def test_tool_advertises_widget_and_supplies_text_fallback(self):
        tool = self.call("tools/list")["tools"][0]
        self.assertEqual(tool["name"], "open_dashboard")
        self.assertEqual(tool["_meta"]["ui"]["resourceUri"], RESOURCE_URI)

        result = self.call("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertEqual(result["structuredContent"]["revision"], "A")
        self.assertIn("demo-local-01", result["content"][0]["text"])

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
        self.assertIn("demo-local-01", result["content"][0]["text"])

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

    def test_environment_path_traversal_override_is_ignored(self):
        result = self.call("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertEqual(result["structuredContent"]["revision"], "A")
        self.assertNotIn("OUTSIDE", result["content"][0]["text"])

    def test_symlink_fixture_target_outside_package_is_rejected(self):
        self.fixture.unlink()
        self.fixture.symlink_to(self.outside_fixture)
        error = self.call_error("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertIn("symlinks", error["message"])

    def test_symlink_fixture_ancestor_outside_package_is_rejected(self):
        outside_dir = self.scratch / "outside-fixtures"
        outside_dir.mkdir()
        (outside_dir / "dashboard.json").write_text(self.outside_fixture.read_text())
        fixture_dir = self.fixture.parent
        fixture_dir.rename(self.package / "fixtures-original")
        fixture_dir.symlink_to(outside_dir, target_is_directory=True)
        error = self.call_error("tools/call", {"name": "open_dashboard", "arguments": {}})
        self.assertIn("symlinks", error["message"])


if __name__ == "__main__":
    unittest.main()
