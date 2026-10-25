"""Live worker evidence, deadline enforcement, and typed usage accounting."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    import worker_observability as obs
except ImportError:
    obs = None
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402


def lines(*events):
    return "\n".join(json.dumps(event) for event in events)


class WorkerObservability(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(obs, "worker observability implementation is required")

    def run_child(self, code, **kwargs):
        return obs.run_streaming([sys.executable, "-u", "-c", code], cwd=None,
                                 env=os.environ.copy(), timeout=kwargs.pop("timeout", 5), **kwargs)

    def test_logs_and_progress_are_readable_while_child_is_alive(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root), secrets=(), attempt=2, job_id="job-2")
            seen = threading.Event()
            result = {}
            code = ('import json,time; print(json.dumps({"type":"item.started",'
                    '"item":{"id":"c1","type":"command_execution"}}),flush=True);'
                    'time.sleep(.8); print("done",flush=True)')
            def run():
                result.update(self.run_child(code, observer=observer))
                seen.set()
            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 3
            progress = {}
            while time.monotonic() < deadline:
                path = Path(root) / "progress.json"
                if path.exists():
                    progress = json.loads(path.read_text())
                    if progress.get("tool_started_count") == 1:
                        break
                time.sleep(.01)
            try:
                self.assertFalse(seen.is_set(), "evidence must be visible before exit")
                self.assertEqual(progress["status"], "tool_started")
                self.assertIn("item.started", (Path(root) / "worker-attempt-2.jsonl").read_text())
                self.assertGreaterEqual(progress["elapsed_seconds"], 0)
                self.assertNotIn("eta", progress)
            finally:
                worker.join(5)
                observer.finish(result=result)
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(observer.snapshot()["status"], "completed")

    def test_one_copy_of_each_attempt_stream_is_persisted(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        report, _ = sp.run_clean_job(root, repo)
        names = sorted(path.name for path in Path(report["report_dir"]).glob("*attempt-1*")
                       if path.name in {"attempt-1.last-message.txt", "attempt-1.prompt.txt",
                                        "worker-attempt-1.jsonl", "worker-attempt-1.stderr.log",
                                        "attempt-1.jsonl", "attempt-1.stderr.txt"})
        self.assertEqual(names, ["attempt-1.last-message.txt", "attempt-1.prompt.txt",
                                 "worker-attempt-1.jsonl", "worker-attempt-1.stderr.log"])
        self.assertTrue(report["codex_home_removed"])
        self.assertFalse(Path(report["codex_home"]).exists())
        self.assertTrue((Path(report["report_dir"]) / sp.lw.codex_catalog.CATALOG_NAME).is_file())

    def test_progress_always_has_phase_and_last_event_time(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            initial = json.loads((Path(root) / "progress.json").read_text())
            self.assertEqual(initial["phase"], initial["status"])
            self.assertIsNone(initial["last_event_at"])
            observer("stdout", b'{"type":"item.started","item":{"id":"c1",'
                     b'"type":"command_execution"}}\n')
            updated = json.loads((Path(root) / "progress.json").read_text())
            self.assertEqual(updated["phase"], "tool_started")
            self.assertIsNotNone(updated["last_event_at"])
            observer.finish()

    def test_real_worker_and_launcher_pids_are_written_to_progress(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            result = self.run_child("import os; print(os.getpid(),flush=True)", observer=observer)
            observer.finish(result=result)
            progress = json.loads((Path(root)/"progress.json").read_text())
            self.assertEqual(progress["launcher_pid"], os.getpid())
            self.assertEqual(progress["worker_pid"], int(result["stdout"].strip()))
            self.assertNotEqual(progress["worker_pid"], progress["launcher_pid"])

    def test_plain_callable_observer_still_receives_process_output(self):
        output = {"stdout": bytearray(), "stderr": bytearray()}
        def observer(stream, chunk):
            output[stream].extend(chunk)
        result = self.run_child('import sys; print("out"); print("err",file=sys.stderr)',
                                observer=observer)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(bytes(output["stdout"]), b"out\n")
        self.assertEqual(bytes(output["stderr"]), b"err\n")

    def test_large_both_pipes_and_stdin_never_deadlock(self):
        code = ('import os,sys; os.write(1,b"x"*200000); '
                'os.write(2,b"y"*200000); data=sys.stdin.buffer.read(); print(len(data))')
        result = self.run_child(code, stdin_data=b"z" * 200000)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stderr"], b"y" * 200000)
        self.assertEqual(result["stdout"], b"x" * 200000 + b"200000\n")
        self.assertFalse(result["timed_out"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires process groups")
    def test_timeout_kills_descendant_even_after_leader_exits_with_pipes_open(self):
        code = ('import os,time,signal; pid=os.fork(); '
                '\nif pid == 0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
                '\nelse:\n print(pid,flush=True); os._exit(0)')
        started = time.monotonic()
        result = self.run_child(code, timeout=.2)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result["termination"]["terminated"])
        self.assertTrue(result["termination"]["force_killed"])
        pid = int(result["stdout"].strip())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # Some hosts keep a dead orphan as a zombie until their init reaps it.
        if Path(f"/proc/{pid}/stat").exists():
            self.assertEqual(Path(f"/proc/{pid}/stat").read_text().split()[2], "Z")
        else:
            self.fail("timed-out descendant survived")

    def test_observer_failure_cleans_up_process_group(self):
        def broken(stream, chunk):
            raise RuntimeError("observer failed")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            self.run_child('import time; print("ready",flush=True); time.sleep(30)', observer=broken)
        self.assertLess(time.monotonic() - started, 3)

    def test_split_secrets_never_reach_persisted_logs_or_progress(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root), secrets=["super-secret-key", "abc", "abcd"],
                                       job_id="super-secret-key")
            observer("stdout", b'prefix super-se')
            self.assertNotIn("super-se", (Path(root) / "worker-attempt-1.jsonl").read_text())
            observer("stdout", b'cret-key abc')
            observer("stdout", b'd tail\n')
            observer("stderr", b'super-secret-')
            observer("stderr", b'key\n')
            observer.finish()
            for path in Path(root).iterdir():
                content = path.read_text()
                self.assertNotIn("super-secret", content)
                self.assertNotIn("abcd", content)
            self.assertEqual((Path(root) / "worker-attempt-1.jsonl").read_text(),
                             "prefix [REDACTED] [REDACTED] tail\n")
            self.assertIn("[REDACTED]", (Path(root) / "worker-attempt-1.stderr.log").read_text())

    def test_tool_completed_and_non_tool_activity_update_progress(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            observer("stdout", (lines({"type":"item.completed", "item":{
                "id":"c1", "type":"command_execution", "status":"completed"}}) + "\n").encode())
            self.assertEqual(observer.snapshot()["status"], "tool_completed")
            observer("stderr", b"still processing\n")
            self.assertEqual(observer.snapshot()["status"], "worker_processing")
            self.assertEqual(observer.snapshot()["tool_completed_count"], 1)
            observer.finish(status="failed")
            self.assertEqual(json.loads((Path(root)/"progress.json").read_text())["status"], "failed")

    def test_finished_elapsed_is_frozen_and_failure_status_uses_actual_result(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root))
            observer.finish(result={"returncode": 1, "timed_out": False})
            before = observer.snapshot()
            time.sleep(.03)
            after = observer.snapshot()
            self.assertEqual(before["elapsed_seconds"], after["elapsed_seconds"])
            self.assertEqual(after["status"], "failed")

    def test_utf8_secret_split_and_large_log_preserve_nonsecret_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            observer = obs.RunObserver(Path(root), secrets=["ключ🔑"])
            encoded = "start ключ🔑 end\n".encode()
            for byte in encoded:
                observer("stdout", bytes([byte]))
            observer("stdout", b"x" * 200000)
            observer.finish()
            self.assertEqual((Path(root)/"worker-attempt-1.jsonl").read_text(),
                             "start [REDACTED] end\n" + "x" * 200000)

    def test_typed_turn_records_sum_once_by_id_and_ignore_quoted_usage(self):
        first = {"type":"turn.completed", "turn_id":"t1", "usage":{
            "input_tokens":11, "output_tokens":7, "cached_input_tokens":3}}
        result = obs.parse_usage(lines(first, first,
            {"type":"turn.completed", "turn_id":"t2", "usage":{
                "input_tokens":13, "output_tokens":5, "cached_input_tokens":2}},
            {"type":"item.completed", "item":{"type":"agent_message", "usage":{
                "input_tokens":9000, "output_tokens":9000}}},
            {"type":"something.new", "usage":{"input_tokens":8000}}))
        self.assertEqual(result["token_counts"], {"input_tokens":24, "output_tokens":12})
        self.assertEqual(result["usage"]["cached_input_tokens"], 5)
        self.assertEqual(result["usage"]["observed_records"], 2)
        self.assertTrue(result["usage"]["complete"])

    def test_per_response_and_token_usage_record_are_supported(self):
        result = obs.parse_usage(lines(
            {"type":"response.completed", "response":{"id":"r1", "usage":{
                "input_tokens":8,"output_tokens":4,"input_tokens_details":{"cached_tokens":2}}}},
            {"type":"token_usage_record", "response_id":"r2", "usage":{
                "input_tokens":3,"output_tokens":1,"cached_input_tokens":0}}))
        self.assertEqual(result["token_counts"], {"input_tokens":11,"output_tokens":5})
        self.assertEqual(result["usage"]["cached_input_tokens"], 2)

    def test_typed_usage_record_payload_wrapper_deduplicates_response_ids(self):
        record = {"type": "token_usage_record", "payload": {
            "response_id": "response-7", "usage": {
                "input_tokens": 17, "output_tokens": 8, "cached_input_tokens": 4}}}
        result = obs.parse_usage(lines(record, record,
            {"type": "item.completed", "item": {"type": "command_execution",
                "payload": record["payload"]}}))
        self.assertEqual(result["token_counts"], {"input_tokens": 17, "output_tokens": 8})
        self.assertEqual(result["usage"]["cached_input_tokens"], 4)
        self.assertEqual(result["usage"]["observed_records"], 1)

    def test_failed_or_timed_out_observer_reports_partial_known_usage(self):
        for finish_kwargs in ({"status": "failed"}, {"status": "timed_out"},
                              {"result": {"returncode": 1}},
                              {"result": {"returncode": -9, "timed_out": True}}):
            with self.subTest(finish_kwargs=finish_kwargs), tempfile.TemporaryDirectory() as root:
                observer = obs.RunObserver(Path(root))
                observer("stdout", (lines({"type": "turn.completed", "usage": {
                    "input_tokens": 5, "output_tokens": 2}}) + "\n").encode())
                self.assertTrue(observer.snapshot()["usage"]["complete"])
                observer.finish(**finish_kwargs)
                usage = observer.snapshot()["usage"]
                self.assertEqual(usage["input_tokens"], 5)
                self.assertEqual(usage["output_tokens"], 2)
                self.assertFalse(usage["complete"])
                self.assertEqual(usage["availability"], "partial")
                self.assertFalse(json.loads((Path(root)/"progress.json").read_text())["usage"]["complete"])

    def test_cumulative_snapshots_are_nonadditive_and_not_added_to_turns(self):
        snapshots = [
            {"type":"event_msg", "payload":{"type":"token_count", "info":{
                "total_token_usage":{"input_tokens":10,"output_tokens":2}}}},
            {"type":"event_msg", "payload":{"type":"token_count", "info":{
                "total_token_usage":{"input_tokens":25,"output_tokens":8}}}}]
        result = obs.parse_usage(lines(*snapshots, snapshots[-1]))
        self.assertEqual(result["token_counts"], {"input_tokens":25,"output_tokens":8})
        self.assertEqual(result["usage"]["source"], "cumulative_snapshot")
        result = obs.parse_usage(lines(*snapshots, {"type":"turn.completed", "usage":{
            "input_tokens":25,"output_tokens":8}}))
        self.assertEqual(result["token_counts"], {"input_tokens":25,"output_tokens":8})

    def test_partial_fields_invalid_values_and_unknown_usage_are_honest(self):
        result = obs.parse_usage(lines(
            {"type":"turn.completed","usage":{"input_tokens":6}},
            {"type":"turn.completed","usage":{"input_tokens":False,"output_tokens":-1}},
            {"type":"turn.completed","usage":{"output_tokens":4}}))
        self.assertEqual(result["token_counts"], {"input_tokens":6,"output_tokens":4})
        self.assertFalse(result["usage"]["complete"])
        self.assertEqual(result["usage"]["availability"], "partial")
        missing = obs.parse_usage('{"type":"agent_message","input_tokens":999}')
        self.assertEqual(missing["token_counts"], {"input_tokens":None,"output_tokens":None})
        self.assertIsNone(missing["usage"]["cached_input_tokens"])
        self.assertEqual(missing["usage"]["availability"], "unavailable")

    def test_attempt_aggregation_preserves_partial_coverage_and_actual_duration(self):
        first = obs.parse_usage(lines({"type":"turn.completed","usage":{
            "input_tokens":10,"output_tokens":4}}))
        second = obs.parse_usage("")
        third = obs.parse_usage(lines({"type":"token_count","usage":{
            "input_tokens":3,"output_tokens":2}}))
        combined = obs.sum_usage([dict(first,elapsed_seconds=1.2),
                                  dict(second,elapsed_seconds=.3), dict(third,elapsed_seconds=2.5)])
        self.assertEqual(combined["token_counts"], {"input_tokens":13,"output_tokens":6})
        self.assertEqual(combined["usage"]["coverage"]["attempts_total"], 3)
        self.assertEqual(combined["usage"]["coverage"]["attempts_with_usage"], 2)
        self.assertEqual(combined["usage"]["elapsed_seconds"], 4.0)
        self.assertFalse(combined["usage"]["complete"])
        self.assertEqual(obs.sum_usage([])["token_counts"], {"input_tokens":None,"output_tokens":None})


if __name__ == "__main__":
    unittest.main()
