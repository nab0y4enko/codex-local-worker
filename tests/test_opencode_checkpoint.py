"""Four-phase OpenCode checkpoint helper: routing, fresh sessions, evidence, failure paths.

Every launch uses a temporary fake executable; no live models, providers or network calls.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opencode_checkpoint.py"
SPEC = importlib.util.spec_from_file_location("opencode_checkpoint", str(SCRIPT))
oc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oc)

CLOUD = "openai/gpt-6.1-sol#high"
LOCAL = "omlx/qwen-3.8-nf-splash"
MARKER = "PACKET-MARKER-8813 accept only when add(2, 3) == 5"

EXPECTED_ROUTING = {"discover": ("plan", LOCAL), "plan": ("plan", CLOUD),
                    "implement": ("build", LOCAL), "review": ("plan", CLOUD)}

FAKE = '''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
record = {"argv0": sys.argv[0], "argv": sys.argv[1:], "cwd": os.getcwd(),
          "pwd": os.environ.get("PWD"),
          "pid": os.getpid(), "pgid": os.getpgid(0), "child_pid": None}
if SLEEP_SECONDS:
    record["child_pid"] = subprocess.Popen(["sleep", "30"]).pid
if IGNORE_TERMINATION:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(RECORD_PATH, "w", encoding="utf-8") as handle:
    json.dump(record, handle)
for line in EVENT_LINES:
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()
if STDERR_TEXT:
    sys.stderr.write(STDERR_TEXT)
    sys.stderr.flush()
if SLEEP_SECONDS:
    time.sleep(SLEEP_SECONDS)
sys.exit(EXIT_CODE)
'''


def text_event(session, text):
    return json.dumps({"type": "message.part.updated",
                       "properties": {"sessionID": session,
                                      "part": {"type": "text", "text": text}}})


def error_event(session, message):
    return json.dumps({"type": "message.part.updated",
                       "properties": {"sessionID": session,
                                      "error": {"name": "ProviderAuthError", "message": message}}})


def native_text_event(session, text):
    """Native CLI envelope: ``type=text`` carrying a ``part`` of ``type=text``."""
    return json.dumps({"type": "text", "timestamp": 1730000000000, "sessionID": session,
                       "part": {"id": "prt_text_1", "sessionID": session,
                                "messageID": "msg_1", "type": "text", "text": text}})


def reasoning_event(session, text):
    """Reasoning part: never assistant response text."""
    return json.dumps({"type": "message.part.updated",
                       "properties": {"sessionID": session,
                                      "part": {"id": "prt_r", "type": "reasoning",
                                               "text": text}}})


def tool_use_event(session, nested_text, nested_error=None):
    """Native ``tool_use`` event whose tool result nests ``type=text`` blocks.

    This is the realistic fixture the parser must not mistake for assistant output: the
    tool result payload carries ``[{"type": "text", "text": ...}]`` in
    ``part.state.metadata.content`` and the tool itself reports an ``error`` object.
    """
    content = [{"type": "text", "text": nested_text}]
    metadata = {"content": content, "exit": 1, "truncated": False}
    if nested_error is not None:
        metadata["error"] = {"name": "ShellError", "message": nested_error,
                             "text": nested_error}
    return json.dumps({"type": "tool_use", "timestamp": 1730000000001, "sessionID": session,
                       "part": {"id": "prt_tool_1", "sessionID": session,
                                "messageID": "msg_2", "type": "tool", "tool": "bash",
                                "callID": "call_7",
                                "state": {"status": "error" if nested_error else "completed",
                                          "input": {"command": "grep -R TODO src"},
                                          "output": json.dumps({"content": content}),
                                          "metadata": metadata}}})


def tool_result_part_event(session, nested_text):
    """The same tool result carried inside a ``message.part.updated`` envelope."""
    return json.dumps({"type": "message.part.updated",
                       "properties": {"sessionID": session,
                                      "part": {"id": "prt_tool_2", "type": "tool",
                                               "tool": "read", "callID": "call_8",
                                               "state": {"status": "completed",
                                                         "input": {"path": "notes.txt"},
                                                         "output": "file body",
                                                         "metadata": {"content": [
                                                             {"type": "text",
                                                              "text": nested_text}]}}}}})


def provider_error_event(session, message):
    """Documented top-level failure envelope from the CLI."""
    return json.dumps({"type": "error", "timestamp": 1730000000002, "sessionID": session,
                       "properties": {"name": "ProviderAuthError", "message": message}})


def process_gone(pid, timeout=2.0):
    """True once the pid no longer exists (killed and reaped by launchd)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class CheckpointHelper(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oc-checkpoint-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.packet = self.root / "packet.md"
        self.packet.write_text("# Task\n\n%s\n" % (MARKER,), encoding="utf-8")

    def fake(self, name, events=(), stderr="", exit_code=0, sleep=0.0, ignore_term=False):
        path = self.root / ("fake-%s" % name)
        source = (FAKE.replace("SLEEP_SECONDS", repr(float(sleep)))
                      .replace("IGNORE_TERMINATION", repr(bool(ignore_term)))
                      .replace("RECORD_PATH", repr(str(self.root / ("record-%s.json" % name))))
                      .replace("EVENT_LINES", repr(list(events)))
                      .replace("STDERR_TEXT", repr(stderr))
                      .replace("EXIT_CODE", repr(int(exit_code))))
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        return path

    def record(self, name):
        path = self.root / ("record-%s.json" % name)
        self.assertTrue(path.is_file(), "fake executable was never launched")
        return json.loads(path.read_text(encoding="utf-8"))

    def invoke(self, phase, output, opencode=None, timeout="60", extra=(), repo=None):
        argv = ["--phase", phase, "--repo", str(repo or self.repo), "--packet", str(self.packet),
                "--output", str(output), "--cloud-model", CLOUD, "--local-model", LOCAL,
                "--timeout", timeout]
        if opencode is not None:
            argv += ["--opencode", str(opencode)]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = oc.main(argv)
        return code, out.getvalue(), err.getvalue()

    def evidence(self, output):
        return json.loads((output / "execution.json").read_text(encoding="utf-8"))

    # ---- routing -------------------------------------------------------------
    def test_every_phase_routes_to_its_fixed_agent_and_model(self):
        for phase, (agent, model) in EXPECTED_ROUTING.items():
            code, printed, _err = self.invoke(phase, self.root / ("dry-%s" % phase),
                                              opencode=self.fake("route"), extra=["--dry-run"])
            self.assertEqual(code, 0, phase)
            report = json.loads(printed)
            self.assertEqual(report["phase"], phase)
            self.assertEqual(report["agent"], agent)
            self.assertEqual(report["model"], model)
            argv = report["argv"]
            self.assertTrue(str(argv[0]))
            self.assertEqual(argv[1:3], ["run", "--standalone"])
            self.assertEqual(argv[argv.index("--agent") + 1], agent)
            self.assertEqual(argv[argv.index("--model") + 1], model)
            self.assertFalse([flag for flag in argv if "config" in flag or "global" in flag])

    def test_routing_is_repeatable_and_touches_no_config(self):
        fake = self.fake("repeat")
        first = json.loads(self.invoke("review", self.root / "d1", opencode=fake,
                                       extra=["--dry-run"])[1])
        second = json.loads(self.invoke("review", self.root / "d2", opencode=fake,
                                        extra=["--dry-run"])[1])
        self.assertEqual(first["argv"][:-1], second["argv"][:-1])
        self.assertEqual(first["prompt_sha256"], second["prompt_sha256"])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        for forbidden in ("--autoapprove", "--dangerously", "--yes", "config write", "opencode.json"):
            self.assertNotIn(forbidden, source)

    def test_every_stage_asks_for_a_new_standalone_session(self):
        output = self.root / "fresh"
        code, _printed, _err = self.invoke("plan", output,
                                           opencode=self.fake("fresh", [text_event("ses_a", "ok")]))
        self.assertEqual(code, 0)
        argv = self.evidence(output)["argv"]
        self.assertIn("--standalone", argv)
        for reused in ("--session", "--continue", "-c", "--fork"):
            self.assertNotIn(reused, argv)
        self.assertEqual(self.evidence(output)["session_ids"], ["ses_a"])

    # ---- pinned working directory -------------------------------------------
    def test_subprocess_cwd_and_env_pwd_are_both_pinned_to_the_repo(self):
        output = self.root / "pwd"
        code, _printed, _err = self.invoke("discover", output,
                                           opencode=self.fake("pwd", [text_event("s1", "evidence")]))
        self.assertEqual(code, 0)
        record = self.record("pwd")
        self.assertEqual(record["cwd"], str(self.repo))
        self.assertEqual(record["pwd"], str(self.repo))
        self.assertEqual(record["argv0"], str(self.root / "fake-pwd"))
        self.assertEqual(record["argv"][0], "run")
        self.assertNotIn("/bin/sh", record["argv"])
        evidence = self.evidence(output)
        self.assertEqual(evidence["cwd_pinned"], str(self.repo))
        self.assertEqual(evidence["pwd_pinned"], str(self.repo))
        self.assertFalse(evidence["shell"])
        self.assertFalse(evidence["autoapprove"])

    # ---- dry run -------------------------------------------------------------
    def test_dry_run_launches_nothing_writes_nothing_and_hides_packet_contents(self):
        output = self.root / "dryrun"
        fake = self.fake("dry", [text_event("s", "nope")])
        code, printed, _err = self.invoke("implement", output, opencode=fake, extra=["--dry-run"])
        self.assertEqual(code, 0)
        report = json.loads(printed)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["filesystem_writes"], 0)
        self.assertFalse(output.exists(), "dry run created the evidence directory")
        self.assertFalse((self.root / "record-dry.json").exists(), "dry run launched a model")
        self.assertNotIn(MARKER, printed)
        self.assertIn("PROMPT", report["argv"][-1])

    def test_dry_run_still_refuses_an_existing_output_directory(self):
        output = self.root / "used"
        output.mkdir()
        (output / "keep.txt").write_text("keep\n", encoding="utf-8")
        code, _printed, err = self.invoke("plan", output, opencode=self.fake("dryused"),
                                          extra=["--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual([item.name for item in output.iterdir()], ["keep.txt"])

    # ---- fresh output refusal before launch ---------------------------------
    def test_existing_output_is_refused_before_the_model_launches(self):
        output = self.root / "occupied"
        output.mkdir()
        (output / "prior.json").write_text("prior\n", encoding="utf-8")
        code, _printed, err = self.invoke("implement", output,
                                          opencode=self.fake("occupied", [text_event("s", "x")]))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertFalse((self.root / "record-occupied.json").exists(),
                         "model was launched onto an existing evidence directory")
        self.assertEqual([item.name for item in output.iterdir()], ["prior.json"])

    # ---- successful evidence -------------------------------------------------
    def test_successful_phase_saves_prompt_events_response_and_execution_record(self):
        output = self.root / "good"
        events = [text_event("ses_cloud", "PART-ONE\n"), text_event("ses_cloud", "PART-TWO")]
        code, _printed, _err = self.invoke("review", output,
                                           opencode=self.fake("good", events, stderr="note to stderr\n"))
        self.assertEqual(code, 0)
        self.assertEqual((output / "response.txt").read_text(encoding="utf-8"), "PART-ONE\nPART-TWO")
        prompt = (output / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Phase: review", prompt)
        self.assertIn(MARKER, prompt)
        self.assertIn("VERDICT: PASS", prompt)
        self.assertEqual((output / "stderr.log").read_text(encoding="utf-8"), "note to stderr\n")
        self.assertEqual(len((output / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        record = self.evidence(output)
        self.assertEqual(record["phase"], "review")
        self.assertEqual(record["agent"], "plan")
        self.assertEqual(record["model"], CLOUD)
        self.assertEqual(record["exit_code"], 0)
        self.assertFalse(record["timed_out"])
        self.assertEqual(record["failure_reasons"], [])
        self.assertEqual(record["session_ids"], ["ses_cloud"])
        self.assertGreaterEqual(record["elapsed_seconds"], 0.0)
        self.assertTrue(record["started_at"] and record["finished_at"])
        for name in record["evidence_files"]:
            self.assertTrue((output / name).is_file(), name)

    # ---- failure paths -------------------------------------------------------
    def test_error_event_fails_and_keeps_raw_events(self):
        output = self.root / "error"
        events = [text_event("ses_err", "partial thought"),
                  error_event("ses_err", "provider rejected the request")]
        code, _printed, _err = self.invoke("plan", output, opencode=self.fake("error", events))
        self.assertEqual(code, 1)
        raw = (output / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("ProviderAuthError", raw)
        record = self.evidence(output)
        self.assertEqual(record["exit_code"], 0)
        self.assertIn("error_event", record["failure_reasons"])
        self.assertEqual(len(record["error_events"]), 1)
        self.assertIn("provider rejected", record["error_events"][0])

    def test_nonzero_exit_fails_even_when_text_was_produced(self):
        output = self.root / "exit3"
        code, _printed, _err = self.invoke("implement", output,
                                           opencode=self.fake("exit3", [text_event("s", "did work")],
                                                              exit_code=3))
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("nonzero_exit", record["failure_reasons"])
        self.assertIn("did work", (output / "response.txt").read_text(encoding="utf-8"))

    def test_run_without_assistant_text_fails(self):
        output = self.root / "silent"
        events = [json.dumps({"type": "step.finish", "properties": {"sessionID": "ses_quiet"}})]
        code, _printed, _err = self.invoke("discover", output, opencode=self.fake("silent", events))
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertIn("no_assistant_text", record["failure_reasons"])
        self.assertFalse(record["response_present"])
        self.assertEqual((output / "response.txt").read_text(encoding="utf-8"), "")
        self.assertEqual(record["session_ids"], ["ses_quiet"])

    def test_missing_executable_fails_and_records_the_launch_error(self):
        output = self.root / "nobin"
        code, _printed, _err = self.invoke("plan", output, opencode=self.root / "not-installed")
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertIn("launch_failed", record["failure_reasons"])
        self.assertIn("FileNotFoundError", record["launch_error"])
        self.assertIn("FileNotFoundError", (output / "stderr.log").read_text(encoding="utf-8"))

    # ---- timeout -------------------------------------------------------------
    def test_timeout_kills_the_process_group_and_retains_partial_evidence(self):
        output = self.root / "slow"
        events = [text_event("ses_slow", "EARLY-EVIDENCE")]
        code, _printed, _err = self.invoke("implement", output,
                                           opencode=self.fake("slow", events, sleep=30.0),
                                           timeout="0.5")
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertTrue(record["timed_out"])
        self.assertIn("timeout", record["failure_reasons"])
        self.assertLess(record["elapsed_seconds"], 15.0)
        self.assertIn("EARLY-EVIDENCE", (output / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIn("EARLY-EVIDENCE", (output / "response.txt").read_text(encoding="utf-8"))
        launch = self.record("slow")
        self.assertEqual(launch["pgid"], launch["pid"], "child did not lead its own process group")
        self.assertNotEqual(launch["pgid"], os.getpgid(0))
        with self.assertRaises(ProcessLookupError):
            os.kill(launch["pid"], 0)
        self.assertNotEqual(launch["child_pid"], launch["pid"])
        self.assertTrue(process_gone(launch["child_pid"]), "group member survived the kill")

    def test_timeout_escalates_to_kill_when_sigterm_is_ignored(self):
        output = self.root / "stubborn"
        code, _printed, _err = self.invoke("review", output,
                                           opencode=self.fake("stubborn", [text_event("s", "hi")],
                                                              sleep=30.0, ignore_term=True),
                                           timeout="0.4")
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertTrue(record["timed_out"])
        self.assertLess(record["elapsed_seconds"], 25.0)
        launch = self.record("stubborn")
        with self.assertRaises(ProcessLookupError):
            os.kill(launch["pid"], 0)

    # ---- assistant text vs tool data -----------------------------------------
    def test_only_native_text_envelopes_count_as_assistant_text(self):
        self.assertEqual(oc.extract_text_parts(json.loads(native_text_event("s", "CLI TEXT"))),
                         ["CLI TEXT"])
        self.assertEqual(oc.extract_text_parts(json.loads(text_event("s", "BUS TEXT"))),
                         ["BUS TEXT"])
        self.assertEqual(oc.extract_text_parts(json.loads(
            tool_use_event("s", "NESTED TOOL TEXT", nested_error="grep exited 1"))), [])
        self.assertEqual(oc.extract_text_parts(json.loads(
            tool_result_part_event("s", "NESTED TOOL TEXT"))), [])
        self.assertEqual(oc.extract_text_parts(json.loads(reasoning_event("s", "hidden thought"))),
                         [])

    def test_tool_payload_errors_are_not_reported_as_provider_errors(self):
        self.assertIsNone(oc.extract_error(json.loads(
            tool_use_event("s", "tool text", nested_error="shell exited 1"))))
        self.assertIsNone(oc.extract_error(json.loads(
            tool_result_part_event("s", "tool text"))))
        self.assertIsNone(oc.extract_error(json.loads(text_event("s", "plain answer"))))
        self.assertIsNone(oc.extract_error(json.loads(native_text_event("s", "plain answer"))))
        self.assertIn("provider rejected the request", oc.extract_error(json.loads(
            error_event("s", "provider rejected the request"))))
        self.assertIn("bad api key", oc.extract_error(json.loads(
            provider_error_event("s", "bad api key"))))

    def test_tool_result_text_alone_is_no_assistant_text_and_no_error_event(self):
        output = self.root / "toolonly"
        events = [tool_use_event("ses_tool", "TOOL-NESTED-TEXT", nested_error="grep exited 1")]
        code, _printed, _err = self.invoke("implement", output,
                                           opencode=self.fake("toolonly", events))
        self.assertEqual(code, 1)
        self.assertEqual((output / "response.txt").read_text(encoding="utf-8"), "")
        record = self.evidence(output)
        self.assertEqual(record["failure_reasons"], ["no_assistant_text"])
        self.assertNotIn("error_event", record["failure_reasons"])
        self.assertEqual(record["error_events"], [])
        self.assertFalse(record["response_present"])
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["parsed_events"], 1)
        self.assertEqual(record["session_ids"], ["ses_tool"])
        self.assertIn("TOOL-NESTED-TEXT", (output / "events.jsonl").read_text(encoding="utf-8"))

    def test_real_text_beside_tool_data_is_the_whole_response_without_error_events(self):
        output = self.root / "mixed"
        events = [tool_use_event("ses_mix", "SHOULD-NOT-APPEAR",
                                 nested_error="nonzero shell exit"),
                  native_text_event("ses_mix", "REAL ANSWER\n"),
                  text_event("ses_mix", " SECOND PART"),
                  tool_result_part_event("ses_mix", "ALSO-NOT-RESPONSE-TEXT")]
        code, _printed, _err = self.invoke("plan", output, opencode=self.fake("mixed", events))
        self.assertEqual(code, 0)
        self.assertEqual((output / "response.txt").read_text(encoding="utf-8"),
                         "REAL ANSWER\n SECOND PART")
        record = self.evidence(output)
        self.assertEqual(record["failure_reasons"], [])
        self.assertEqual(record["error_events"], [])
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["event_lines"], 4)
        raw = (output / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("SHOULD-NOT-APPEAR", raw)
        response = (output / "response.txt").read_text(encoding="utf-8")
        self.assertNotIn("SHOULD-NOT-APPEAR", response)

    def test_top_level_provider_error_still_fails_and_is_summarized(self):
        output = self.root / "provider"
        events = [native_text_event("ses_p", "thinking out loud"),
                  provider_error_event("ses_p", "bad api key for openai/gpt-6.1-sol")]
        code, _printed, _err = self.invoke("review", output,
                                           opencode=self.fake("provider", events))
        self.assertEqual(code, 1)
        record = self.evidence(output)
        self.assertIn("error_event", record["failure_reasons"])
        self.assertEqual(len(record["error_events"]), 1)
        self.assertIn("bad api key for openai/gpt-6.1-sol", record["error_events"][0])
        self.assertIn("ProviderAuthError", (output / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((output / "response.txt").read_text(encoding="utf-8"),
                         "thinking out loud")

    def test_recorded_secrets_note_puts_responsibility_on_the_packet(self):
        output = self.root / "secretsnote"
        fake = self.fake("note", [text_event("s", "ok")])
        code, _printed, _err = self.invoke("discover", output, opencode=fake)
        self.assertEqual(code, 0)
        notes = [self.evidence(output)["argv_secrets"].lower()]
        _code, printed, _err = self.invoke("discover", self.root / "secretsnote-dry",
                                           opencode=fake, extra=["--dry-run"])
        notes.append(json.loads(printed)["argv_secrets"].lower())
        for note in notes:
            self.assertIn("keep secrets out of the packet", note)
            self.assertIn("never dumped", note)
            self.assertNotIn("no secrets", note)
            self.assertNotIn("cannot contain", note)

    # ---- argument validation -------------------------------------------------
    def test_bad_arguments_are_rejected_before_any_output_directory_is_made(self):
        cases = [
            (["--repo", "relative/path"], "absolute"),
            (["--repo", str(self.root / "missing")], "existing directory"),
            (["--packet", str(self.root / "nope.md")], "not an existing file"),
            (["--cloud-model", "openai/gpt-no-variant"], "provider/model#variant"),
            (["--local-model", LOCAL + "#medium"], "without #variant"),
            (["--timeout", "0"], "greater than zero"),
            (["--timeout", "nan"], "finite"),
            (["--timeout", "inf"], "finite"),
            (["--timeout", "Infinity"], "finite"),
            (["--timeout", "-0.5"], "greater than zero"),
        ]
        for override, expected in cases:
            output = self.root / ("reject-%s" % abs(hash(tuple(override))))
            argv = ["--phase", "plan", "--repo", str(self.repo), "--packet", str(self.packet),
                    "--output", str(output), "--cloud-model", CLOUD, "--local-model", LOCAL,
                    "--opencode", str(self.fake("reject")), "--timeout", "60"]
            for index in range(0, len(override), 2):
                argv[argv.index(override[index]) + 1] = override[index + 1]
            out, errbuf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
                code = oc.main(argv)
            self.assertEqual(code, 2, override)
            self.assertIn(expected, errbuf.getvalue())
            self.assertFalse(output.exists(), override)
        self.assertFalse((self.root / "record-reject.json").exists())

    def test_nonfinite_timeout_is_rejected_before_any_output_or_launch(self):
        for value in ("nan", "inf", "Infinity"):
            output = self.root / ("badtimeout-%s" % value)
            code, printed, err = self.invoke("plan", output,
                                             opencode=self.fake("badtimeout"), timeout=value)
            self.assertEqual(code, 2, value)
            self.assertIn("finite", err, value)
            self.assertEqual(printed, "", value)
            self.assertFalse(output.exists(), value)
        self.assertFalse((self.root / "record-badtimeout.json").exists(),
                         "a nonfinite --timeout still launched the model")
        code, printed, err = self.invoke("plan", self.root / "badtimeout-dry",
                                         opencode=self.fake("badtimeoutdry"),
                                         timeout="nan", extra=["--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("finite", err)
        self.assertEqual(printed, "")
        self.assertFalse((self.root / "badtimeout-dry").exists())
        self.assertFalse((self.root / "record-badtimeoutdry.json").exists())

    def test_missing_git_checkout_is_refused_without_touching_the_output(self):
        plain = self.root / "not-a-repo"
        plain.mkdir()
        output = self.root / "reject-git"
        fake = self.fake("rejectgit")
        code, _printed, err = self.invoke("discover", output, opencode=fake, repo=plain)
        self.assertEqual(code, 2)
        self.assertIn("not a Git checkout", err)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "record-rejectgit.json").exists())


if __name__ == "__main__":
    unittest.main()
