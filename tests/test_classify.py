import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import classify
import local_worker as lw
import support as sp


def terminal_error(event_type, message):
    event = {"type": event_type, "error": {"message": message}}
    return lw.typed_worker_error_text(json.dumps(event))


CASES = [
    ({"status": "ok"}, ("ok", "done")),
    ({"status": "worker_failed", "attempts": [{"error_text": terminal_error("error", "unsupported call: apply_patch"), "returncode": 1}]}, ("tool_unavailable", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"terminal_failures": ["turn.failed"], "error_text": terminal_error("turn.failed", "context length exceeded")}]}, ("context_overflow", "retry_local")),
    ({"status": "answer_invalid", "kind": "read", "attempts": [{"returncode": 1,
      "error_text": terminal_error("error", "Prompt is too long: 893958 tokens > maximum context length")} ]},
     ("context_overflow", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"stalled": True}]}, ("stall", "retry_local")),
    ({"status": "tests_failed", "attempts": [{}, {}], "max_attempts": 2}, ("tests_failed", "escalate_cloud")),
    ({"status": "audit_violation", "violations": [{"path": "x", "reason": "outside allowed_paths"}]}, ("out_of_scope", "escalate_cloud")),
    ({"status": "worker_failed", "attempts": [{"sandbox_denial": {
        "source": "sandboxed_command_output", "kind": "read", "path": "~/.npmrc",
        "known_secret": True}}]}, ("secret_read_denied", "needs_user")),
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


    def test_worker_busy_is_retryable_without_verification(self):
        result = classify.classify({"status": "worker_busy", "attempts": []})
        self.assertEqual((result.code, result.next_action), ("worker_busy", "retry_local"))
        self.assertIn("--lock-timeout", result.remediation)

    def test_recovered_historical_stall_does_not_override_success(self):
        report = {"status": "ok", "budget": {"exceeded": False}, "attempts": [
            {"stalled": True, "returncode": 1},
            {"stalled": False, "timed_out": False, "returncode": 0},
        ]}
        result = classify.classify(report)
        self.assertEqual((result.cls, result.code, result.next_action), ("none", "ok", "done"))

    def test_historical_stall_does_not_mask_latest_capability_failure(self):
        report = {"status": "tests_failed", "budget": {"exceeded": False}, "attempts": [
            {"stalled": True, "returncode": 1},
            {"stalled": False, "timed_out": False, "returncode": 0},
        ], "tests": [{"status": "failed", "returncode": 1}]}
        result = classify.classify(report)
        self.assertEqual((result.cls, result.code, result.next_action),
                         ("capability", "tests_failed", "escalate_cloud"))

    def test_server_crash_mid_attempt_is_environment(self):
        error = terminal_error("error", "error sending request: Connection refused (os error 61)")
        report = {"status": "tests_failed", "attempts": [{"returncode": 1, "error_text": error}]}
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
                                        "classification", "answer", "answer_evidence", "patch_path", "screenshots",
                                        "tests", "tokens", "durations", "report_path"])

    def test_classifier_ignores_worker_prose(self):
        result = classify.classify({"status": "worker_failed", "failure_reason": "I think the server was down"})
        self.assertEqual(result.code, "verification_blocked")

    def test_typed_sandbox_denial_is_actionable_without_relaxing_permissions(self):
        result = classify.classify({"status": "worker_failed", "kind": "read",
            "attempts": [{"returncode": 1, "sandbox_denial": {
                "source": "codex_terminal_error", "kind": "read", "path": "/private/data.txt"}}]})
        self.assertEqual((result.cls, result.code, result.next_action),
                         ("environment", "sandbox_denied", "needs_user"))
        self.assertIn("keep", result.remediation.lower())

    def test_launcher_promotes_only_parsed_known_read_and_protected_write_denials(self):
        perms = lw.permissions.default_permissions()
        known_read = lw.typed_sandbox_denial([{"path": str(Path.home() / ".ssh" / "id_ed25519"),
                                               "kind": "read", "cause": "deny_read"}],
                                              perms, source="sandboxed_command_output")
        self.assertIs(known_read["known_secret"], True)
        protected_write = lw.typed_sandbox_denial([{"path": "/repo/private", "kind": "write",
                                                     "cause": "primary_checkout"}],
                                                    perms, source="sandboxed_command_output")
        self.assertIs(protected_write["known_out_of_scope"], True)
        unknown = lw.typed_sandbox_denial([{"path": "/elsewhere/private", "kind": "read",
                                            "cause": "deny_read"}], perms,
                                          source="sandboxed_command_output")
        self.assertIs(unknown["known_secret"], False)

    def test_sandbox_denial_does_not_classify_from_worker_prose_or_raw_stderr(self):
        result = classify.classify({"status": "worker_failed", "failure_reason": "Permission denied",
            "attempts": [{"returncode": 1, "stderr_excerpt": "Permission denied /private/data.txt"}]})
        self.assertNotEqual(result.code, "sandbox_denied")

    def test_untyped_global_read_denials_and_blocked_out_of_scope_writes_are_not_misreported(self):
        global_only = classify.classify({"status": "worker_failed", "permissions": {
            "denials": [{"kind": "read", "path": "/private/file"}]}})
        self.assertNotEqual(global_only.code, "secret_read_denied")
        blocked_write = classify.classify({"status": "worker_failed", "attempts": [{
            "returncode": 1, "sandbox_denial": {"source": "sandboxed_command_output",
                "kind": "write", "path": "/repo/secret", "known_out_of_scope": True}}]})
        self.assertEqual(blocked_write.code, "sandbox_denied")
        self.assertIn("out-of-scope", " ".join(blocked_write.evidence))

    def test_raw_stderr_infrastructure_phrases_are_not_classification_evidence(self):
        report = {"status": "worker_failed", "attempts": [{"returncode": 1,
                  "stderr_excerpt": "Connection refused; context length exceeded; unsupported call"}]}
        result = classify.classify(report)
        self.assertEqual((result.cls, result.code, result.next_action),
                         ("environment", "verification_blocked", "needs_user"))

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
