"""Job orchestration with a mocked worker subprocess, status rules, and process control.

Nothing here talks to a model or to a server: the worker and the sandbox probe are
stand-ins for inference (``support.FakeRun``), every git call is a real disposable
repository, and oMLX discovery is a recorded fake (``support.FakeOmlx``).
"""
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
FIX = "def add(a, b):\n    return a + b\n"


class StatusDecision(unittest.TestCase):
    passed = [{"status": "passed", "command": ["python3", "-m", "unittest"], "sandboxed": True}]
    attempt = {"attempt": 1, "returncode": 0, "timed_out": False, "model_routing": "ok",
               "unsupported_tools": False}

    def report(self, **overrides):
        report = {"status": "worker_failed", "attempts": [dict(self.attempt)], "tests": self.passed,
                  "violations": [], "primary_unchanged": True, "api_model": sp.MODEL,
                  "budget": {"exceeded": False}}
        report.update(overrides)
        return report

    def test_success_needs_every_signal(self):
        self.assertEqual(lw.decide_status(self.report(), True), "ok")

    def test_rejections(self):
        blocked = [{"status": "blocked", "command": ["python3"], "returncode": None,
                    "reason": "sandbox unavailable"}]
        failed = [{"status": "failed", "command": ["python3"], "returncode": 1}]
        cases = [
            ({"primary_unchanged": False}, True, "audit_violation"),
            ({"primary_unchanged": False, "tests": self.passed}, True, "audit_violation"),
            ({"violations": [{"path": "notes.md", "reason": "outside allowed_paths"}]}, True,
             "audit_violation"),
            ({"budget": {"exceeded": True}, "tests": []}, True, "timeout"),
            ({"budget": {"exceeded": True}, "tests": blocked}, True, "timeout"),
            ({"attempts": [dict(self.attempt, timed_out=True, returncode=-9)], "tests": failed},
             False, "timeout"),
            ({"tests": blocked}, True, "verification_blocked"),
            ({"tests": [{"status": "skipped", "command": ["python3"]}]}, True,
             "verification_blocked"),
            ({"tests": []}, True, "verification_blocked"),
            ({"tests": self.passed + failed}, True, "tests_failed"),
            ({"attempts": [dict(self.attempt, model_routing="mismatch",
                                observed_models=["gpt-oss-120b"])]}, True, "model_mismatch"),
            ({"attempts": [dict(self.attempt, unsupported_tools=True)]}, True, "worker_failed"),
            ({"attempts": [dict(self.attempt, returncode=1, model_routing="absent")]}, False,
             "worker_failed"),
        ]
        for index, (overrides, worker_ok, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(lw.decide_status(self.report(**overrides), worker_ok), expected)

    def test_every_non_ok_status_explains_itself(self):
        for status in [name for name in lw.EXIT_CODES if name != "ok"]:
            report = self.report(status=status, tests=[{"status": "blocked", "command": ["pytest"],
                                                        "returncode": None}], primary_unchanged=False)
            report["attempts"] = [dict(self.attempt, returncode=1, unsupported_tools=True,
                                       model_routing="mismatch", observed_models=["gpt-oss-120b"],
                                       stderr_excerpt="unsupported tool", timed_out=False)]
            reason = lw.describe_failure(report)
            self.assertTrue(reason and "None" not in reason, status)
        self.assertEqual(lw.describe_failure(self.report(status="ok")), "")
        self.assertIn("no verification evidence",
                      lw.describe_failure(self.report(status="timeout", attempts=[], tests=[])))
        for status in lw.EXIT_CODES:
            report = self.report(status=status, attempts=[], tests=[], primary_unchanged=True)
            self.assertTrue(lw.describe_failure(report) or status == "ok", status)


class Processes(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)

    def test_worker_lock_is_exclusive_and_released(self):
        with lw.job_lock(self.root):
            self.assertIn(str(os.getpid()), (self.root / "worker.lock").read_text())
            with self.assertRaises(lw.BlockedError):
                with lw.job_lock(self.root, timeout=0.2):
                    pass
        with lw.job_lock(self.root, timeout=0.5):
            pass

    def test_run_bounded_captures_output_and_kills_on_deadline(self):
        outcome = lw.run_bounded(["/bin/sh", "-c", "echo out; echo err 1>&2; exit 3"], cwd=None,
                                 env=dict(os.environ), timeout=10)
        self.assertEqual((outcome["returncode"], outcome["timed_out"]), (3, False))
        self.assertEqual((outcome["stdout"].strip(), outcome["stderr"].strip()), (b"out", b"err"))
        outcome = lw.run_bounded(["/bin/sh", "-c", "sleep 30"], cwd=None, env=dict(os.environ),
                                 timeout=0.3)
        self.assertTrue(outcome["timed_out"])
        self.assertTrue(outcome["termination"]["terminated"])
        self.assertFalse(outcome["termination"]["survivors"])
        self.assertEqual(outcome["returncode"], -signal.SIGTERM)

    def test_children_that_ignore_sigterm_are_force_killed(self):
        (self.root / "ignorer.sh").write_text("#!/bin/sh\ntrap '' TERM\nsleep 30\n")
        (self.root / "ignorer.sh").chmod(0o755)
        cases = [(["/bin/sh", "-c", 'trap "" TERM; sleep 30'], True),
                 (["/bin/sh", "-c", '(trap "" TERM; sleep 30) & wait'], True),
                 (["/bin/sh", str(self.root / "ignorer.sh")], True),
                 (["/bin/sh", "-c", "sleep 30"], False)]
        started = time.monotonic()
        with mock.patch.object(lw, "TERMINATE_GRACE_SECONDS", 0.2), \
                mock.patch.object(lw, "KILL_GRACE_SECONDS", 3.0):
            for argv, forced in cases:
                with self.subTest(argv=argv[-1][:24]):
                    outcome = lw.run_bounded(argv, cwd=None, env=dict(os.environ), timeout=0.3)
                    self.assertTrue(outcome["timed_out"])
                    self.assertTrue(outcome["termination"]["terminated"])
                    self.assertEqual(outcome["termination"]["force_killed"], forced)
                    self.assertFalse(outcome["termination"]["survivors"])
        self.assertLess(time.monotonic() - started, 6.0)


class RunJob(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        (self.repo / "sentinel.txt").write_text("dirty before the job\n")
        self.options = sp.job_options(self.root, self.repo)

    def run_job(self, fake, options=None):
        with mock.patch.object(lw, "run_bounded", fake):
            return lw.run_job(options or self.options)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX)

    def report_json(self, report):
        return Path(report["report_dir"], "report.json").read_text(encoding="utf-8")

    def test_clean_job_is_verified_and_persists_evidence(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix, worker_stderr="leak " + sp.SECRET))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["changed_paths"], ["add.py"])
        self.assertEqual(report["token_counts"], {"input_tokens": 11, "output_tokens": 7})
        self.assertTrue(Path(report["worktree"], "add.py").read_text().endswith("a + b\n"))
        self.assertTrue(Path(report["worktree"]).is_dir())          # preserved, never merged
        self.assertEqual(lw.rev_parse(self.repo, "HEAD"), report["base_commit"])
        self.assertIn("+    return a + b", Path(report["patch_path"]).read_text())
        text = self.report_json(report)
        self.assertNotIn(sp.SECRET, text)
        self.assertIn("[REDACTED]", text)

    def test_successful_run_reports_preflight_evidence_without_the_key(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix))
        preflight = report["preflight"]
        self.assertEqual(preflight["endpoint"], "http://127.0.0.1:8000/v1")
        self.assertEqual(preflight["profile"]["model"]["api_model"], sp.MODEL)
        self.assertEqual(preflight["profile"]["model"]["canonical_id"], sp.MODEL)
        self.assertEqual(preflight["context_window"], sp.MODEL_WINDOW)
        self.assertIs(preflight["model_fallback"], False)
        self.assertNotIn(sp.SECRET, json.dumps(preflight))
        self.assertNotIn(sp.SECRET, str(report["logs"]))
        self.assertEqual(report["sandbox"]["backend"], lw.FULL_HOST_BACKEND)
        self.assertEqual(report["sandbox"]["profile"], lw.FULL_HOST_PROFILE)
        self.assertEqual(report["sandbox"]["network"], "enabled")

    def test_primary_checkout_untouched_is_proved_by_content_hashes(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix))
        self.assertTrue(report["primary_unchanged"])
        self.assertEqual(report["dirty_primary_files"], ["sentinel.txt"])
        self.assertEqual(report["primary_fingerprint_before"], report["primary_fingerprint_after"])
        self.assertEqual(report["primary_fingerprint_before"]["contents"]["sentinel.txt"],
                         lw._digest_path(self.repo / "sentinel.txt"))

    def test_editing_the_primary_checkout_is_a_violation_even_with_passing_tests(self):
        def edit(cwd, attempt):
            self.fix(cwd)
            (self.repo / "sentinel.txt").write_text("touched during the job\n")
        # Without the deny list nothing stopped the worker writing the primary checkout, so a
        # change there fails the job (with it applied the change is reported, not attributed).
        with mock.patch.object(lw.permissions, "wrapper_available", return_value=False):
            report = self.run_job(sp.FakeRun(worker_edit=edit))
        self.assertEqual(report["status"], "audit_violation")
        self.assertFalse(report["primary_unchanged"])
        self.assertNotEqual(report["primary_fingerprint_before"], report["primary_fingerprint_after"])
        self.assertIn("primary checkout changed", report["failure_reason"])
        self.assertEqual([test["status"] for test in report["tests"]], ["passed"])

    def test_out_of_scope_edit_is_detected_after_the_run(self):
        def edit(cwd, attempt):
            self.fix(cwd)
            Path(cwd, "notes.md").write_text("sneaky\n")
        report = self.run_job(sp.FakeRun(worker_edit=edit))
        self.assertEqual(report["status"], "audit_violation")
        self.assertEqual([item["path"] for item in report["violations"]], ["notes.md"])
        self.assertIn("outside allowed_paths", report["failure_reason"])

    def test_failed_preflight_still_persists_a_report_and_launches_nothing(self):
        options = sp.job_options(self.root / "pf", self.repo, omlx=sp.FakeOmlx("none_loaded"))
        fake = sp.FakeRun()
        report = self.run_job(fake, options)
        self.assertEqual(report["status"], "preflight_failed")
        self.assertIsNone(report["worktree"])
        self.assertEqual(fake.calls, [])
        self.assertIn(sp.MODEL, json.loads(self.report_json(report))["failure_reason"])

    def test_state_dir_inside_the_repository_is_refused(self):
        options = sp.job_options(self.root, self.repo)
        options.state_dir = self.repo / "state"
        report = lw.run_job(options)
        self.assertEqual(report["status"], "contract_invalid")
        self.assertIn("outside the repository", report["failure_reason"])
        self.assertFalse((self.repo / "state").exists())

    def test_report_directory_exists_before_preflight_runs(self):
        report = lw.new_report(self.options)
        with self.assertRaises(lw.DeadlineExpired):
            lw._execute(self.options, report, lw.Deadline(0.0))
        self.assertTrue(Path(report["report_dir"]).is_dir())
        self.assertEqual(report["preflight"], {})

    def test_exhausted_budget_is_a_persisted_timeout(self):
        expired = lw.Deadline(-1.0)
        with mock.patch.object(lw, "Deadline", lambda total: expired):
            report = lw.run_job(self.options)
        self.assertEqual(report["status"], "timeout")
        self.assertIn("budget", report["failure_reason"])
        self.assertTrue(Path(report["report_dir"], "report.json").is_file())

    def test_failing_tests_and_deadlines_are_never_success(self):
        report = self.run_job(sp.FakeRun(test_rc=1, worker_edit=self.fix))
        self.assertEqual(report["status"], "tests_failed")
        self.assertIn("unittest", report["failure_reason"])
        report = self.run_job(sp.FakeRun(worker_timed_out=True, worker_edit=self.fix))
        self.assertEqual(report["status"], "timeout")
        self.assertIn("killed", report["failure_reason"])
        report = self.run_job(sp.FakeRun(probe_ok=False, worker_edit=self.fix))
        self.assertEqual(report["status"], "verification_blocked")
        self.assertEqual(len(report["tests"]), 1)
        self.assertFalse(report["tests"][0]["sandboxed"])

    def test_second_attempt_receives_feedback(self):
        options = sp.job_options(self.root / "tw", self.repo, max_attempts=2)
        fake = sp.FakeRun(test_rc=1, worker_edit=self.fix)
        report = self.run_job(fake, options)
        self.assertEqual(report["status"], "tests_failed")
        self.assertEqual(fake.exec_calls, 2)
        self.assertIn("Previous attempt feedback", fake.calls_of("exec")[1]["stdin"])
        self.assertEqual(len(report["attempts"]), 2)

    def test_local_key_stays_out_of_the_model_shell_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-cloud",
                                          "HTTP_PROXY": "http://corp:3128"}):
            fake = sp.FakeRun(worker_edit=self.fix)
            report = self.run_job(fake)
        self.assertEqual(report["status"], "ok")
        exec_call = fake.calls_of("exec")[0]
        self.assertEqual(exec_call["env"][lw.API_KEY_ENV], sp.SECRET)
        self.assertNotIn("OPENAI_API_KEY", exec_call["env"])
        self.assertNotIn("HTTP_PROXY", exec_call["env"])
        verification = [call for call in fake.calls if call["argv"][1:2] != ["exec"]]
        self.assertTrue(verification)                                  # full host: ran directly
        for call in verification:
            self.assertNotIn(lw.API_KEY_ENV, call["env"])
        self.assertEqual(report["environment"]["proxies_stripped"], ["HTTP_PROXY"])
        self.assertIn("OPENAI_API_KEY", report["environment"]["cloud_credential_vars_removed"])


if __name__ == "__main__":
    unittest.main()
