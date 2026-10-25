import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import classify
import local_worker as lw
import support as sp


CASES = [
    ({"status": "ok"}, ("ok", "done")),
    ({"status": "worker_failed", "attempts": [{"stderr_excerpt": "error=unsupported call: apply_patch", "returncode": 1}]}, ("tool_unavailable", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"terminal_failures": ["turn.failed"], "error_text": "context length exceeded"}]}, ("context_overflow", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"stalled": True}]}, ("stall", "retry_local")),
    ({"status": "tests_failed", "attempts": [{}, {}], "max_attempts": 2}, ("tests_failed", "escalate_cloud")),
    ({"status": "audit_violation", "violations": [{"path": "x", "reason": "outside allowed_paths"}]}, ("out_of_scope", "escalate_cloud")),
    ({"status": "worker_failed", "permissions": {"denials": [{"path": "~/.npmrc", "kind": "read"}]}}, ("secret_read_denied", "needs_user")),
    ({"status": "answer_invalid", "citations": {"total": 3, "verified": 1}}, ("unverified_answer", "escalate_cloud")),
    ({"status": "preflight_failed", "discovery_error": {"code": "model_ambiguous"}}, ("model_ambiguous", "needs_user")),
]


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)

    def test_synthetic_reports_classify_from_launcher_evidence(self):
        for report, expected in CASES:
            with self.subTest(expected=expected):
                result = classify.classify(report)
                self.assertEqual((result.code, result.next_action), expected)

    def test_server_crash_mid_attempt_is_environment(self):
        report = {"status": "tests_failed", "attempts": [{"returncode": 1, "stderr_excerpt": "error sending request: Connection refused (os error 61)"}]}
        result = classify.classify(report)
        self.assertEqual((result.cls, result.code, result.next_action), ("environment", "server_unreachable", "retry_local"))

    def test_untyped_failed_worker_exit_uses_job_outcome_without_reading_prose(self):
        attempt = {"returncode": 1}
        write = classify.classify({"status": "worker_failed", "kind": "write",
                                   "worker_patch_present": False, "attempts": [attempt]})
        self.assertEqual((write.cls, write.code), ("capability", "no_change"))
        read = classify.classify({"status": "worker_failed", "kind": "read",
                                  "attempts": [attempt]})
        self.assertEqual((read.cls, read.code), ("capability", "invalid_answer"))
        patched = classify.classify({"status": "worker_failed", "kind": "write",
                                     "worker_patch_present": True, "attempts": [attempt]})
        self.assertEqual((patched.cls, patched.code), ("capability", "tests_failed"))

    def test_retry_local_is_offered_once_per_code(self):
        report = dict(CASES[1][0], previous_codes=["tool_unavailable"])
        self.assertEqual(classify.classify(report).next_action, "needs_user")

    def test_failed_citation_is_unverified_answer_even_if_schema_valid(self):
        report = {"status": "answer_invalid", "answer_valid": True,
                  "citations": {"total": 2, "verified": 1, "failures": ["quote mismatch"]}}
        result = classify.classify(report)
        self.assertEqual((result.cls, result.code), ("capability", "unverified_answer"))
        self.assertIn("citation", " ".join(result.evidence).lower())

    def test_planner_json_exposes_only_the_stable_summary_keys(self):
        result = lw.planner_json({"job_id": "j", "kind": "read", "tier": "local",
                                  "api_model": "local-model", "status": "ok",
                                  "classification": {"class": "none", "code": "ok"},
                                  "report_dir": "/tmp/report"})
        self.assertEqual(list(result), ["job_id", "kind", "tier", "model", "status",
                                        "classification", "answer", "patch_path", "screenshots",
                                        "tests", "tokens", "durations", "report_path"])

    def test_classifier_ignores_worker_prose(self):
        result = classify.classify({"status": "worker_failed", "failure_reason": "I think the server was down"})
        self.assertEqual(result.code, "verification_blocked")

    def test_context_overflow_gets_one_lower_cap_retry(self):
        first = '{"type":"turn.failed","error":{"message":"context length exceeded"}}\n'
        run = sp.FakeRun(worker_edit=sp.fix_add, first_stream=first)
        report, run = sp.run_clean_job(self.root, self.repo, fake_run=run)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["auto_retries"], [{"code": "context_overflow", "context_window": int(262144 * .75)}])
        self.assertEqual(run.exec_calls, 2)

    def test_distinct_safe_fixes_each_retry_once_within_the_job(self):
        streams = [
            '{"type":"turn.failed","error":{"message":"context length exceeded"}}\n',
            '{"type":"error","error":{"message":"Connection refused"}}\n',
        ]
        run = sp.FakeRun(worker_edit=sp.fix_add, attempt_streams=streams)
        report, run = sp.run_clean_job(self.root, self.repo, fake_run=run)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([item["code"] for item in report["auto_retries"]],
                         ["context_overflow", "server_unreachable"])
        self.assertEqual(run.exec_calls, 3)

    def test_automatic_retry_does_not_repeat_a_failure_code(self):
        stream = '{"type":"turn.failed","error":{"message":"context length exceeded"}}\n'
        run = sp.FakeRun(worker_edit=sp.fix_add, attempt_streams=[stream, stream])
        report, run = sp.run_clean_job(self.root, self.repo, fake_run=run)
        self.assertEqual(report["status"], "worker_failed")
        self.assertEqual([item["code"] for item in report["auto_retries"]], ["context_overflow"])
        self.assertEqual(run.exec_calls, 2)


if __name__ == "__main__":
    unittest.main()
