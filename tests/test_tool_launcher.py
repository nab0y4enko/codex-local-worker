"""``--tool-config`` end to end: selected MCP argv, child env, redaction, real observations."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
TOKEN = "mcp-token-0123456789abcdef"
VAR = "TASK_MCP_TOKEN"
TOO_SHORT = "q7!"
FIX = "def add(a, b):\n    return a + b\n"


def tool_event(server, tool):
    return {"type": "item.completed",
            "item": {"type": "mcp_tool_call", "server": server, "tool": tool}}


def message(text):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


class ToolRun(sp.FakeRun):
    """FakeRun plus the JSONL a worker would emit for the tools it actually used."""

    def __init__(self, events=(), **kwargs):
        super().__init__(**kwargs)
        self.events = list(events)

    def __call__(self, argv, **kwargs):
        outcome = super().__call__(argv, **kwargs)
        if self.events and self.calls[-1]["argv"][1:2] == ["exec"]:
            outcome["stdout"] += "".join(json.dumps(item) + "\n" for item in self.events).encode()
        return outcome


class ToolLauncher(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.prog = self.bin / "ctx-mcp"
        self.prog.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.prog.chmod(0o755)
        self.options = sp.job_options(self.root, self.repo)

    def manifest(self, **spec):
        server = {"command": str(self.prog), "enabled_tools": ["read_file"], "env_vars": [VAR]}
        server.update(spec)
        path = self.root / "tools.json"
        path.write_text(json.dumps({"schema_version": 1, "mcp_servers": {"ctx": server}}),
                        encoding="utf-8")
        self.options.tool_config = path
        return path

    def run_job(self, fake, spec=None, environ=None, goal=None, probe=None):
        self.manifest(**(spec or {})) if spec is not False else None
        if goal:
            self.options.task = lw.Task.from_payload(sp.task_payload(self.repo, goal=goal))
        with mock.patch.dict(os.environ, {VAR: TOKEN} if environ is None else environ), \
                mock.patch.object(lw, "run_bounded", fake), \
                mock.patch.object(lw, "probe_tools",
                                  probe or self.probed):
            return lw.run_job(self.options)

    @staticmethod
    def probed(loaded, **kwargs):
        """The PREPROBE is mocked here: the fake codex binary cannot answer an MCP handshake."""
        return loaded["evidence"]

    def files(self, report, name):
        return Path(report["report_dir"], name).read_text(encoding="utf-8")

    def artifacts(self, report):
        """Every persisted artifact of a job, keyed by its name: nothing may hold a secret."""
        found = {}
        for path in sorted(Path(report["report_dir"]).rglob("*")):
            if path.is_file():
                found[path.name] = path.read_text(encoding="utf-8", errors="replace")
        return found

    def test_a_tool_that_cannot_be_probed_blocks_before_the_worker_starts(self):
        fake = sp.FakeRun()
        refusal = lw.ToolProbeError("mcp server 'ctx' did not advertise required tool(s): read_file")
        report = self.run_job(fake, {}, probe=mock.Mock(side_effect=refusal))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.calls_of("exec"), [])            # no inference was spent
        self.assertIn("did not advertise required tool", report["failure_reason"])

    def test_selected_mcp_reaches_worker_argv_and_env_but_not_verification(self):
        fake = sp.FakeRun(worker_edit=lambda cwd, attempt: Path(cwd, "add.py").write_text(FIX))
        report = self.run_job(fake, {})
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        worker = fake.calls_of("exec")[0]
        joined = " ".join(worker["argv"])
        self.assertIn("mcp_servers.ctx.command=%s" % json.dumps(str(self.prog)), joined)
        self.assertIn('mcp_servers.ctx.env_vars=["TASK_MCP_TOKEN"]', joined)
        self.assertIn(VAR, joined)                                  # name only, never the value
        self.assertNotIn(TOKEN, joined)
        self.assertEqual(worker["env"][VAR], TOKEN)                 # the MCP server inherits it
        profile = sp.discovered_profile()                              # provider pins preserved
        self.assertEqual(lw.missing_config_pins(worker["argv"], profile,
                                                profile.model.context_window), [])
        excluded = json.loads([item.split("=", 1)[1] for item in worker["argv"]
                               if item.startswith(lw.SHELL_ENV_EXCLUDES_KEY + "=")][0])
        self.assertIn(VAR, excluded)
        self.assertIn(lw.API_KEY_ENV, excluded)
        for call in [call for call in fake.calls if call["argv"][1:2] != ["exec"]]:
            self.assertNotIn(VAR, call["env"])                      # verification never sees it
            self.assertNotIn(lw.API_KEY_ENV, call["env"])
        self.assertEqual([server for server in report["tooling"]["servers"]], ["ctx"])

    def test_credential_never_reaches_any_report_artifact(self):
        # The token is echoed by the worker on stdout and stderr, and the worker puts it into
        # the file it edits, so the report, the logs and the patch all have to be redacted.
        leaked = FIX + "# token " + TOKEN + "\n"
        fake = ToolRun(events=[message("token " + TOKEN)], worker_stderr="auth " + TOKEN,
                       worker_edit=lambda cwd, attempt: Path(cwd, "add.py").write_text(leaked))
        report = self.run_job(fake, {}, goal="Wire %s through the client." % TOKEN)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        artifacts = self.artifacts(report)
        self.assertIn("changes.patch", artifacts)
        self.assertEqual(report["patch_path"], str(Path(report["report_dir"], "changes.patch")))
        for name in ("attempt-1.jsonl", "attempt-1.stderr.txt", "attempt-1.prompt.txt",
                     "report.json", "changes.patch"):
            self.assertIn(name, artifacts)
            self.assertNotIn(TOKEN, artifacts[name], name)
            self.assertIn("[REDACTED]", artifacts[name], name)
        for name, text in artifacts.items():
            self.assertNotIn(TOKEN, text, name)
        # A validation failure whose own message quotes the manifest command is redacted too.
        blocked = self.run_job(sp.FakeRun(), {"command": "/nonexistent/tool-" + TOKEN})
        self.assertEqual(blocked["status"], "blocked")
        self.assertNotIn(TOKEN, self.files(blocked, "report.json"))
        self.assertIn("[REDACTED]", blocked["failure_reason"])
        for name, text in self.artifacts(blocked).items():
            self.assertNotIn(TOKEN, text, name)

    def test_validation_error_that_quotes_the_command_is_blocked_and_redacted(self):
        fake = sp.FakeRun()
        report = self.run_job(fake, {"command": TOKEN, "env_vars": [VAR]})
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.calls_of("exec"), [])
        self.assertIn("[REDACTED]", report["failure_reason"])
        for name, text in self.artifacts(report).items():
            self.assertNotIn(TOKEN, text, name)

    def test_explicit_tool_call_is_observed_and_a_quoted_name_is_not(self):
        fake = ToolRun(events=[tool_event("ctx", "read_file"),
                               message("I called read_file and write_file"),
                               tool_event("ctx", "write_file")],
                       worker_edit=lambda cwd, attempt: Path(cwd, "add.py").write_text(FIX))
        report = self.run_job(fake, {})
        tooling = report["tooling"]
        self.assertEqual(tooling["observed_tools"], [{"server": "ctx", "tool": "read_file"}])
        self.assertIs(tooling["servers"]["ctx"]["observed"], True)

    def test_missing_command_blocks_the_job_without_starting_a_worker(self):
        fake = sp.FakeRun()
        report = self.run_job(fake, {"command": "ctx-mcp-absent"})
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.calls_of("exec"), [])
        self.assertIn("was not found on the supplied PATH", report["failure_reason"])

    def test_short_credential_is_rejected_without_leaking_its_value(self):
        fake = sp.FakeRun()
        report = self.run_job(fake, {}, environ={VAR: TOO_SHORT})
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(fake.calls_of("exec"), [])
        self.assertIn("too short to redact", report["failure_reason"])
        self.assertNotIn(TOO_SHORT, self.files(report, "report.json"))


if __name__ == "__main__":
    unittest.main()
