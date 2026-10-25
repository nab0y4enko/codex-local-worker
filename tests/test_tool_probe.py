"""Required-tool PREPROBE: fake app-server JSONL, no model turn, bounded cleanup, real fixture."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

import worker_tool_probe as probe  # noqa: E402

lw = sp.lw
TOKEN = "fake-upstream-secret-token"
MODEL_KEY = "local-model-key-super-secret-0123456789"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_echo.py"

FAKE = """#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
secret = "__SECRET__"
with open(os.environ["FAKE_PID"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
methods = []
init = None


def record():
    with open(os.environ["FAKE_LOG"], "w", encoding="utf-8") as handle:
        json.dump({"methods": methods, "argv": sys.argv, "cwd": os.getcwd(),
                   "codex_home": os.environ.get("CODEX_HOME"),
                   "init": init,
                   "model_key": "LOCAL_OMLX_API_KEY" in os.environ,
                   "tool_token": os.environ.get("TASK_MCP_TOKEN") == secret}, handle)


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


record()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    methods.append(message.get("method"))
    if method == "initialize":
        init = message.get("params")
    record()
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        pass
    elif method == "mcpServerStatus/list":
        name = message["params"]["serverName"]
        if mode == "stall":
            # one page whose cursor is larger than a pipe, then stop reading: the probe has to
            # echo that cursor back, so its write must stay bounded by the probe deadline
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "data": [{"name": name, "toolsError": None,
                          "tools": {"write_file": {"name": "write_file"},
                                    "read_file": {"name": "read_file"}}}],
                "nextCursor": "c" * int(os.environ.get("FAKE_CURSOR_BYTES", "2097152"))}})
            while True:
                time.sleep(60)
        elif mode == "flood":
            while True:                 # endless notifications: the total stdout cap must bite
                send({"jsonrpc": "2.0", "method": "log", "params": {"note": "n" * 1024}})
        elif mode == "timeout":
            time.sleep(30)
        elif mode == "toolserror":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"data": [{"name": name, "toolsError": "handshake failed: " + secret}]}})
        elif mode == "missing":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"data": [{"name": name, "tools": {"read_file": {"name": "read_file"}}}]}})
        elif mode == "absent":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"data": []}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"data": [
                {"name": name, "toolsError": None,
                 "tools": {"write_file": {"name": "write_file"}, "read_file": {"name": "read_file"}}}]}})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601,
              "message": "unexpected method " + secret}})
"""


class FakeAppServer(unittest.TestCase):
    """The probe transport against a scripted app-server: it never talks to a model."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.report = self.root / "report"
        self.report.mkdir()
        self.home = lw.job_codex_home(self.report)
        self.environ = dict(os.environ)

    def tearDown(self):
        self.assertEqual(dict(os.environ), self.environ)   # the worker env is never repointed

    def loaded(self, tools=("read_file", "write_file")):
        return {"overrides": ["-c", 'mcp_servers.ctx.enabled_tools=["read_file","write_file"]'],
                "evidence": {"schema_version": 1, "servers": {"ctx": {
                    "configured": True, "probed": False, "observed": False, "transport": "stdio",
                    "enabled_tools": list(tools), "startup_timeout_sec": 5, "tool_timeout_sec": 60,
                    "command": "/bin/true"}}}}

    def run_probe(self, mode="ok", tools=("read_file", "write_file"), seconds=20):
        script = self.root / "fake-codex"
        script.write_text(FAKE.replace("__SECRET__", TOKEN), encoding="utf-8")
        script.chmod(0o755)
        self.log = self.root / "log.json"
        self.pidfile = self.root / "pid"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root),
               "FAKE_MODE": mode, "FAKE_LOG": str(self.log), "FAKE_PID": str(self.pidfile),
               "TASK_MCP_TOKEN": TOKEN, lw.API_KEY_ENV: MODEL_KEY, "CODEX_HOME": str(self.root)}
        loaded = self.loaded(tools)
        result = probe.probe_tools(loaded, codex_bin=str(script), env=env,
                                   overrides=lw.config_overrides(sp.discovered_profile(),
                                                                 sp.MODEL_WINDOW),
                                   deadline=lw.Deadline(seconds), codex_home=self.home)
        return loaded, result

    def log_data(self):
        for _ in range(100):
            if self.log.exists():
                return json.loads(self.log.read_text(encoding="utf-8"))
            time.sleep(0.02)
        self.fail("the fake app-server never recorded a request")

    def test_read_profile_wraps_app_server_process(self):
        profile = self.report / "mcp.sb"
        profile.write_text("(version 1)\n(allow default)\n(deny file-write*)\n",
                           encoding="utf-8")
        fake_process = mock.Mock()
        with mock.patch.object(probe.subprocess, "Popen", return_value=fake_process) as popen:
            probe._spawn("/usr/bin/codex", [], {"overrides": []},
                         {"CODEX_HOME": str(self.home)}, str(self.home), profile)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/sandbox-exec", "-f", str(profile)])
        self.assertEqual(command[3:6], ["/usr/bin/codex", "app-server", "--listen"])

    def assert_probe_group_gone(self):
        """The whole probe session, group included, must not survive a refusal."""
        pid = int(self.pidfile.read_text(encoding="utf-8"))
        for _ in range(200):
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail("the probe process group survived its deadline")

    def test_probe_uses_codex_status_only_and_marks_the_catalog(self):
        loaded, result = self.run_probe()
        meta = result["servers"]["ctx"]
        self.assertIs(meta["probed"], True)
        self.assertIs(meta["configured"], True)
        self.assertIs(meta["observed"], False)
        self.assertEqual(meta["discovered_tools"], ["read_file", "write_file"])
        self.assertIs(loaded["evidence"]["servers"]["ctx"]["probed"], False)  # a copy, always
        record = self.log_data()
        self.assertEqual(record["methods"], ["initialize", "initialized", "mcpServerStatus/list"])
        for forbidden in ("thread/start", "turn/start", "thread/", "turn/", "tools/call"):
            self.assertFalse([item for item in record["methods"] if item and item.startswith(forbidden)])
        self.assertEqual(record["argv"][1:4], ["app-server", "--listen", "stdio://"])
        self.assertIn("mcp_servers={}", record["argv"])
        self.assertIn('mcp_servers.ctx.enabled_tools=["read_file","write_file"]', record["argv"])
        self.assertEqual(record["init"], {"clientInfo": {"name": "local_worker_tool_probe",
                                                           "version": "1"}})
        joined = " ".join(record["argv"])
        self.assertIn('model_provider="local-omlx"', joined)          # the worker's own pins
        self.assertIn("features.plugins=false", joined)               # stay off for the probe
        self.assertFalse(record["model_key"])                  # metadata needs no model auth
        self.assertTrue(record["tool_token"])                  # the server credential is inherited
        # The probe runs in the job's own Codex home, never the caller's (``self.root`` above).
        self.assertEqual(record["codex_home"], str(self.home))
        self.assertEqual(Path(record["cwd"]), self.home.resolve())
        self.assertEqual([item.name for item in self.report.iterdir()], ["codex-home"])

    def test_missing_configured_tool_is_refused_naming_the_server(self):
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("missing")
        message = str(caught.exception)
        self.assertIn("ctx", message)
        self.assertIn("write_file", message)
        self.assertNotIn(TOKEN, message)

    def test_server_status_error_is_refused_without_the_upstream_text(self):
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("toolserror")
        self.assertIn("ctx", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_unreported_server_is_refused(self):
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("absent")
        self.assertIn("did not report mcp server 'ctx'", str(caught.exception))

    def test_timeout_terminates_the_whole_probe_group(self):
        started = time.monotonic()
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("timeout", seconds=2)
        self.assertIn("did not answer", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10)
        self.assert_probe_group_gone()

    def test_stalled_reader_and_oversized_cursor_are_refused_within_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("stall", seconds=3)
        message = str(caught.exception)
        self.assertIn("deadline", message)
        self.assertNotIn(TOKEN, message)
        self.assertLess(time.monotonic() - started, 15)
        self.assert_probe_group_gone()

    def test_endless_notifications_cannot_outrun_the_total_or_deadline(self):
        started = time.monotonic()
        with self.assertRaises(probe.ToolProbeError) as caught:
            self.run_probe("flood", seconds=20)
        self.assertIn(str(probe.MAX_TOTAL_BYTES), str(caught.exception))
        self.assertLess(time.monotonic() - started, 15)
        self.assert_probe_group_gone()

    def test_no_servers_probes_nothing_and_returns_the_evidence_unchanged(self):
        loaded = {"overrides": [], "evidence": {"schema_version": 1, "servers": {}}}
        self.assertEqual(probe.probe_tools(loaded, codex_bin="/nonexistent/codex", env={},
                                           overrides=[], deadline=lw.Deadline(30),
                                           codex_home=self.home), loaded["evidence"])

    def test_a_missing_job_home_is_refused_before_anything_starts(self):
        with self.assertRaises(probe.ToolProbeError) as caught:
            probe.probe_tools(self.loaded(), codex_bin="/nonexistent/codex", env={},
                              overrides=[], deadline=lw.Deadline(30),
                              codex_home=self.report / "absent")
        self.assertIn("Codex home", str(caught.exception))


class McpEchoFixture(unittest.TestCase):
    """The real stdlib fixture a pinned worker qualification run will use: token-gated echo tool."""

    def exchange(self, process, request):
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        while True:
            answer = json.loads(process.stdout.readline())
            if answer.get("method"):                 # notifications are not answers
                continue
            return answer

    def start(self, env):
        process = subprocess.Popen([sys.executable, str(FIXTURE)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def test_initialize_list_and_call_the_echo_tool(self):
        process = self.start({"TASK_TOOL_TOKEN": TOKEN, "PATH": os.environ.get("PATH", "/usr/bin")})
        first = self.exchange(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2024-11-05",
                                                  "capabilities": {}}})
        self.assertEqual(first["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(first["result"]["serverInfo"], {"name": "fixture", "version": "1"})
        self.assertEqual(first["result"]["capabilities"], {"tools": {}})
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
                            + "\n")
        process.stdin.flush()
        listed = self.exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                        "params": {}})
        tool = listed["result"]["tools"][0]
        self.assertEqual(tool["name"], "echo")
        self.assertEqual(tool["inputSchema"]["properties"]["text"], {"type": "string"})
        self.assertEqual(tool["inputSchema"]["required"], ["text"])
        called = self.exchange(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "echo", "arguments": {"text": "hi"}}})
        self.assertEqual(called["result"]["content"], [{"type": "text", "text": "fixture:hi"}])
        self.assertIs(called["result"]["isError"], False)
        refused = self.exchange(process, {"jsonrpc": "2.0", "id": 4, "method": "turn/start",
                                         "params": {}})
        self.assertIn("error", refused)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertNotIn(TOKEN, process.stderr.read())

    def test_fixture_refuses_to_start_without_its_token(self):
        process = self.start({"PATH": os.environ.get("PATH", "/usr/bin")})
        self.assertEqual(process.wait(timeout=10), 2)
        self.assertIn("TASK_TOOL_TOKEN", process.stderr.read())


if __name__ == "__main__":
    unittest.main()
