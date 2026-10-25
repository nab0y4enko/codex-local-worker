"""Read jobs run read-only and only release answers with verified citations."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support as sp
import contract
import local_worker as lw


class AnswerRun(sp.FakeRun):
    def __init__(self, last_message, **kwargs):
        super().__init__(worker_edit=None, **kwargs)
        self.last_message = last_message

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        argv, _ = sp.deny_wrapped([str(value) for value in argv])
        if argv[1:2] == ["exec"] and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(self.last_message, encoding="utf-8")
        return result


class ReadJobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(sp.tmpdir())
        self.repo = sp.make_repo(self.root / "repo")

    def tearDown(self):
        sp.cleanup(self.root)

    def payload(self):
        return {"task_id": "read-task", "kind": "read", "repo_path": str(self.repo),
                "base_ref": "main", "goal": "Find the add implementation", "timeout_seconds": 90,
                "question": "What does add do?", "scope_paths": ["add.py"],
                "answer_schema": {"type": "object", "required": ["summary"],
                                  "additionalProperties": False,
                                  "properties": {"summary": {"type": "string"}}},
                "max_answer_bytes": 4096}

    def run_read_job(self, bad_citation=False, max_bytes=4096, worker_rc=0,
                     secret_output=None):
        citation = {"path": "add.py", "line_start": 1 if not bad_citation else 9,
                    "line_end": 1 if not bad_citation else 9,
                    "quote": "def add" if not bad_citation else "missing"}
        summary = secret_output or "add currently subtracts"
        message = json.dumps({"answer": {"summary": summary},
                              "citations": [citation]})
        fake = AnswerRun(message, worker_rc=worker_rc)
        payload = self.payload()
        if secret_output:
            payload["environment"] = {"pass": ["JAVA_HOME"]}
        task = contract.Contract.from_payload(payload)
        task.max_answer_bytes = max_bytes
        options = lw.JobOptions(task=task, state_dir=self.root / "state", codex_bin="codex-mock",
                                settings_path=sp.write_settings(self.root / "omlx/settings.json"),
                                omlx_fetch=sp.FakeOmlx("one"), verify=True,
                                codex_run=sp.fake_help())
        from unittest import mock
        with mock.patch.object(lw, "run_bounded", fake):
            if secret_output:
                with mock.patch.dict(os.environ, {"JAVA_HOME": secret_output}):
                    report = lw.run_job(options)
            else:
                report = lw.run_job(options)
        return report, fake

    def test_read_job_returns_only_a_verified_answer(self):
        report, run = self.run_read_job(bad_citation=True)
        self.assertEqual(report["status"], "answer_invalid")
        self.assertNotIn("answer", report)
        worker = run.calls_of("exec")[0]["argv"]
        self.assertEqual(worker[worker.index("-s") + 1], "read-only")
        self.assertIn('sandbox_mode="read-only"', worker)
        self.assertIn("--output-schema", worker)
        schema_path = Path(worker[worker.index("--output-schema") + 1])
        self.assertTrue(schema_path.is_file())
        self.assertEqual(run.verification_calls(), [])

    def test_valid_read_job_returns_answer_without_a_patch(self):
        report, run = self.run_read_job()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["answer"], {"summary": "add currently subtracts"})
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(run.verification_calls(), [])

    def test_oversized_answer_is_not_released(self):
        report, _ = self.run_read_job(max_bytes=8)
        self.assertEqual(report["status"], "answer_invalid")
        self.assertNotIn("answer", report)
        self.assertIn("max_answer_bytes", report["failure_reason"])

    def test_nonzero_worker_exit_does_not_release_an_otherwise_valid_answer(self):
        report, _ = self.run_read_job(worker_rc=1)
        self.assertEqual(report["status"], "answer_invalid")
        self.assertNotIn("answer", report)
        self.assertEqual(report["citations"], [])

    def test_passed_environment_values_are_redacted_from_returned_read_report(self):
        secret = "/path/to/private-jdk"
        report, run = self.run_read_job(secret_output=secret)
        self.assertEqual(run.calls_of("exec")[0]["env"]["JAVA_HOME"], secret)
        self.assertNotIn(secret, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
