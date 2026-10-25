"""Worker event evidence: terminal failures, explicit identity, deduplicated tool lifecycle.

Every assertion here pins a documented event location. Quoted or recursively nested
payload data must never become routing, thread, tool or failure evidence.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
obs = lw.observation
ALIAS = sp.MODEL
OTHER = "some-other-model"
CLEAN = {"model": ALIAS, "type": "token_count",
         "usage": {"input_tokens": 11, "output_tokens": 7}}
PASSED = [{"status": "passed", "command": ["python3"], "sandboxed": True}]


def stream(*events):
    return "".join(f"{event if isinstance(event, str) else json.dumps(event)}\n"
                   for event in events)


def evidence(*events):
    return lw.parse_worker_log(stream(*events))


def observer_events(observer, *events):
    observer("stdout", stream(*events).encode())


def report(**overrides):
    base = {"status": "worker_failed", "attempts": [], "tests": PASSED, "violations": [],
            "primary_unchanged": True, "api_model": ALIAS, "budget": {"exceeded": False}}
    base.update(overrides)
    return base


class StreamRun(sp.FakeRun):
    """FakeRun with a worker stream the test controls; everything else stays mocked alike."""

    def __init__(self, worker_stream, **kwargs):
        super().__init__(**kwargs)
        self.worker_stream = worker_stream

    def __call__(self, argv, *, cwd, env, timeout, stdin_data=None, stall_seconds=None,
                 on_stall=None):
        raw = [str(item) for item in argv]
        argv, wrapped = sp.deny_wrapped(raw)
        if argv[1:2] == ["exec"]:
            self.calls.append({"argv": argv, "raw_argv": raw, "wrapped": wrapped, "kind": "exec",
                               "cwd": Path(cwd) if cwd else None,
                               "env": dict(env), "timeout": timeout,
                               "stdin": (stdin_data or b"").decode("utf-8", "replace")})
            self.exec_calls += 1
            if self.worker_edit:
                self.worker_edit(Path(cwd), self.exec_calls)
            return sp.result(argv, self.worker_rc, self.worker_stream, self.worker_stderr,
                             timed_out=self.worker_timed_out)
        return super().__call__(raw, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
                                stall_seconds=stall_seconds, on_stall=on_stall)


class TerminalEventEvidence(unittest.TestCase):
    def test_generic_terminal_events_are_failures_with_their_own_type(self):
        variants = (
            {"type": "turn.failed", "error": {"message": "the turn was aborted"}},
            {"type": "response.failed", "response": {"error": {"message": "upstream rejected"}}},
            {"type": "error", "message": "stream disconnected before the final message"})
        for index, event in enumerate(variants):
            with self.subTest(event=index):
                parsed = evidence(CLEAN, event)
                self.assertTrue(parsed["terminal_failure"])
                self.assertEqual(parsed["terminal_failures"], [event["type"]])
                self.assertEqual(parsed["observed_models"], [ALIAS])

    def test_terminal_failure_is_never_reported_ok_with_a_zero_exit(self):
        attempt = {"attempt": 1, "returncode": 0, "timed_out": False, "model_routing": "ok",
                   "unsupported_tools": False, "terminal_failures": ["turn.failed"]}
        for worker_ok in (True, False):
            with self.subTest(worker_ok=worker_ok):
                self.assertEqual(lw.decide_status(report(attempts=[dict(attempt)]), worker_ok),
                                 "worker_failed")
                described = lw.describe_failure(report(attempts=[dict(attempt)], status="worker_failed"))
                self.assertIn("turn.failed", described)

    def test_recovered_tool_failures_are_not_terminal_worker_failures(self):
        parsed = evidence(
            CLEAN,
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "status": "failed", "exit_code": 1,
                                                "aggregated_output": "FAILED 1 test\n"}},
            {"type": "item.completed", "item": {"id": "c2", "type": "command_execution",
                                                "status": "completed", "exit_code": 0,
                                                "aggregated_output": "Ran 1 test OK\n"}})
        self.assertFalse(parsed["terminal_failure"])
        self.assertEqual(parsed["terminal_failures"], [])
        self.assertEqual(parsed["tool_completed_count"], 2)

    def test_payload_text_that_quotes_terminal_events_is_not_a_failure(self):
        parsed = evidence(
            CLEAN,
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "status": "completed", "exit_code": 0,
                                                "aggregated_output": stream(
                                                    {"type": "turn.failed",
                                                     "error": {"message": "quoted by rg"}})}},
            {"type": "item.completed", "item": {"id": "c2", "type": "file_read",
                                                "content": '{"type":"error","message":"quoted"}'}},
            {"type": "event_msg", "payload": {"type": "agent_message",
                                              "message": "the log contains response.failed"}})
        self.assertFalse(parsed["terminal_failure"])
        self.assertEqual(parsed["terminal_failures"], [])
        self.assertFalse(parsed["unsupported_tools"])

    def test_successful_stream_has_no_terminal_failure(self):
        parsed = evidence({"type": "thread.started", "thread_id": "thr_1"}, CLEAN,
                          {"type": "turn.completed", "usage": {"input_tokens": 3,
                                                               "output_tokens": 1}})
        self.assertFalse(parsed["terminal_failure"])
        self.assertEqual(parsed["terminal_failures"], [])

    def test_terminal_worker_event_stops_the_job_from_being_ok_end_to_end(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        worker = stream({"type": "thread.started", "thread_id": "thr_real123"},
                        CLEAN,
                        {"type": "turn.failed", "error": {"message": "turn aborted"}},
                        {"type": "error", "message": "worker halted"})
        fake = StreamRun(worker, worker_edit=lambda cwd, n: (cwd / "add.py").write_text("fixed\n"))
        with mock.patch.object(lw, "run_bounded", fake):
            report_data = lw.run_job(sp.job_options(root, repo))
        attempt = report_data["attempts"][0]
        self.assertEqual(report_data["status"], "worker_failed")
        self.assertEqual(attempt["returncode"], 0)
        self.assertTrue(all(test["status"] == "passed" for test in report_data["tests"]))
        self.assertEqual(attempt["terminal_failures"], ["error", "turn.failed"])
        self.assertEqual(attempt["thread_id"], "thr_real123")
        self.assertIn("turn.failed", report_data["failure_reason"])
        progress = json.loads((Path(report_data["report_dir"]) / "progress.json").read_text())
        self.assertEqual(progress["thread_id"], "thr_real123")
        self.assertEqual(progress["status"], "worker_failed")


class ModelIdentityEvidence(unittest.TestCase):
    def test_quoted_or_nested_model_names_are_not_routing_evidence(self):
        parsed = evidence(
            CLEAN,
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "status": "completed",
                                                "aggregated_output": stream(
                                                    {"model": OTHER})}},
            {"type": "item.completed", "item": {"id": "c2", "type": "file_read",
                                                "content": "model = " + OTHER,
                                                "metadata": {"model": OTHER}}},
            {"type": "event_msg", "payload": {"type": "agent_message",
                                              "message": "switched to " + OTHER}},
            {"type": "error", "message": "rejected", "details": {"model": OTHER}})
        self.assertEqual(parsed["observed_models"], [ALIAS])
        self.assertEqual(lw.model_routing(parsed, ALIAS), "ok")

    def test_only_payload_model_data_is_absent_evidence(self):
        parsed = evidence({"type": "item.completed", "item": {"id": "c1", "type": "file_read",
                                                              "metadata": {"model": OTHER}}},
                          {"type": "item.completed", "item": {"id": "c2",
                                                              "type": "command_execution",
                                                              "arguments": {"model": OTHER}}})
        self.assertEqual(parsed["observed_models"], [])
        self.assertEqual(lw.model_routing(parsed, ALIAS), "absent")

    def test_top_level_and_response_metadata_are_routing_evidence(self):
        self.assertEqual(lw.model_routing(evidence(CLEAN), ALIAS), "ok")
        response = evidence({"type": "response.completed",
                             "response": {"id": "r1", "model": ALIAS,
                                          "usage": {"input_tokens": 4, "output_tokens": 2}}})
        self.assertEqual(response["observed_models"], [ALIAS])
        self.assertEqual(lw.model_routing(response, ALIAS), "ok")
        self.assertEqual(lw.model_routing(evidence({"type": "turn.started", "model": OTHER}),
                                          ALIAS), "mismatch")


class ThreadIdentityEvidence(unittest.TestCase):
    def test_actual_thread_id_survives_progress_and_completion(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            self.assertIsNone(observer.snapshot()["thread_id"])
            observer_events(observer, {"type": "thread.started", "thread_id": "thr_live_42"})
            self.assertEqual(observer.snapshot()["thread_id"], "thr_live_42")
            observer.finish(result={"returncode": 0, "timed_out": False})
            self.assertEqual(json.loads((Path(root) / "progress.json").read_text())["thread_id"],
                             "thr_live_42")
            snapshot = observer.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["thread_id"], "thr_live_42")
            self.assertEqual(json.loads((Path(root) / "progress.json").read_text())["thread_id"],
                             "thr_live_42")

    def test_missing_or_quoted_thread_id_stays_unknown(self):
        variants = (
            {"type": "thread.started"},
            {"type": "thread.started", "thread_id": ""},
            {"type": "thread.started", "item": {"thread_id": "thr_nested_wrong"}},
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "aggregated_output": stream(
                                                    {"type": "thread.started",
                                                     "thread_id": "thr_quoted_wrong"})}})
        for index, event in enumerate(variants):
            with self.subTest(event=index):
                with tempfile.TemporaryDirectory() as root:
                    observer = obs.RunObserver(Path(root))
                    observer_events(observer, CLEAN, event)
                    observer.finish(result={"returncode": 0, "timed_out": False})
                    self.assertIsNone(observer.snapshot()["thread_id"])
                    self.assertIsNone(json.loads(
                        (Path(root) / "progress.json").read_text())["thread_id"])
                    self.assertIsNone(evidence(CLEAN, event)["thread_id"])

    def test_parse_worker_log_reports_the_actual_thread_id(self):
        parsed = evidence({"type": "thread.started", "thread_id": "thr_9f2c"}, CLEAN)
        self.assertEqual(parsed["thread_id"], "thr_9f2c")
        self.assertIsNone(evidence(CLEAN)["thread_id"])


class ToolLifecycleEvidence(unittest.TestCase):
    def test_observer_deduplicates_lifecycle_by_item_id(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            observer_events(observer,
                            {"type": "item.started", "item": {"id": "c1",
                                                              "type": "command_execution"}},
                            {"type": "item.started", "item": {"id": "c1",
                                                              "type": "command_execution"}},
                            {"type": "item.completed", "item": {"id": "c1",
                                                                "type": "command_execution",
                                                                "status": "completed"}},
                            {"type": "item.completed", "item": {"id": "c1",
                                                                "type": "command_execution",
                                                                "status": "completed"}},
                            {"type": "item.started", "item": {"id": "c2", "type": "web_search"}})
            snapshot = observer.snapshot()
            self.assertEqual(snapshot["tool_started_count"], 2)
            self.assertEqual(snapshot["tool_completed_count"], 1)
            self.assertEqual(snapshot["status"], "tool_started")
            self.assertEqual(json.loads(
                (Path(root) / "progress.json").read_text())["tool_started_count"], 2)

    def test_observer_ignores_nested_type_values(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            observer_events(observer,
                            {"type": "item.completed", "item": {"id": "r1", "type": "reasoning",
                                                                "summary": [{"type": "summary_text",
                                                                            "text": "plan"}]}},
                            {"type": "event_msg", "payload": {"type": "item", "item": {
                                "type": "command_execution"}}},
                            {"type": "item.completed", "item": {"id": "c1",
                                                                "type": "command_execution",
                                                                "payload": {"type": "function_call",
                                                                            "name": "rg"}}})
            snapshot = observer.snapshot()
            self.assertEqual(snapshot["tool_started_count"], 0)
            self.assertEqual(snapshot["tool_completed_count"], 1)

    def test_parse_worker_log_counts_distinct_tool_items(self):
        deduplicated = evidence(CLEAN,
                                {"type": "item.started", "item": {"id": "c1",
                                                                  "type": "command_execution"}},
                                {"type": "item.started", "item": {"id": "c1",
                                                                  "type": "command_execution"}},
                                {"type": "item.completed", "item": {"id": "c1",
                                                                    "type": "command_execution",
                                                                    "status": "completed"}})
        self.assertEqual(deduplicated["tool_call_events"], 1)
        self.assertEqual(deduplicated["tool_started_count"], 1)
        self.assertEqual(deduplicated["tool_completed_count"], 1)

    def test_parse_worker_log_ignores_nested_and_non_tool_types(self):
        nested = evidence(CLEAN, {"type": "item.completed", "item": {"id": "c1",
                                                                    "type": "command_execution",
                                                                    "aggregated_output": stream(
                                                                        {"type": "function_call",
                                                                         "name": "rg"})}})
        self.assertEqual(nested["tool_call_events"], 1)
        reasoning = evidence(CLEAN, {"type": "item.completed", "item": {"id": "r1",
                                                                       "type": "reasoning",
                                                                       "summary": [
                                                                           {"type": "summary_text",
                                                                            "text": "x"}]}})
        self.assertEqual(reasoning["tool_call_events"], 0)
        self.assertEqual(reasoning["tool_started_count"], 0)
        self.assertEqual(evidence(CLEAN, {"type": "function_call", "name": "read_file"})
                         ["tool_call_events"], 1)


class UnknownMetadataStaysUnknown(unittest.TestCase):
    def test_empty_stream_reports_every_unknown_as_unknown(self):
        parsed = lw.parse_worker_log("")
        self.assertEqual(parsed["observed_models"], [])
        self.assertIsNone(parsed["thread_id"])
        self.assertEqual(parsed["tool_call_events"], 0)
        self.assertEqual(parsed["tool_started_count"], 0)
        self.assertEqual(parsed["tool_completed_count"], 0)
        self.assertFalse(parsed["terminal_failure"])
        self.assertEqual(parsed["token_counts"], {"input_tokens": None, "output_tokens": None})
        self.assertEqual(parsed["usage"]["availability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
