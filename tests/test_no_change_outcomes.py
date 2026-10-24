"""Explicit no-change outcomes: passing baseline tests alone never proves an implementation.

A clean worker that edited nothing in scope, verification that passes because nothing changed,
and a verification command that writes an allowed file itself all end ``no_changes`` (exit 10).
Real failure signals keep priority over that verdict, and tool observations survive a retry that
called nothing.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
tools = __import__("worker_tools")
wl = __import__("worktree_lifecycle")
FIX = "def add(a, b):\n    return a + b\n"
PASSED = [{"status": "passed", "command": ["python3", "-m", "unittest"], "sandboxed": False}]
BLOCKED = [{"status": "blocked", "command": ["python3"], "returncode": None, "reason": "no probe"}]
FAILED = [{"status": "failed", "command": ["python3"], "returncode": 1}]
ATTEMPT = {"attempt": 1, "returncode": 0, "timed_out": False, "model_routing": "ok",
           "unsupported_tools": False, "has_scoped_patch": True}


def report(**overrides):
    base = {"status": "worker_failed", "attempts": [dict(ATTEMPT)], "tests": PASSED,
            "violations": [], "primary_unchanged": True, "api_model": sp.MODEL,
            "budget": {"exceeded": False}, "worker_patch_present": False}
    base.update(overrides)
    return base


def call(server, tool, event="item.completed"):
    return json.dumps({"type": event, "item": {"type": "mcp_tool_call", "server": server,
                                               "tool": tool}})


class VerifierWrites(sp.FakeRun):
    """Verification alone writes an allowed file; the worker itself never edits."""

    def __call__(self, argv, **kwargs):
        argv = [str(item) for item in argv]
        verification = not (argv[0] == lw.apple.SANDBOX_EXEC
                            or argv == list(lw.FULL_HOST_PROBE_ARGV)
                            or argv[1:2] in (["exec"], ["sandbox"]))
        if verification:
            Path(kwargs["cwd"], "add.py").write_text(FIX, encoding="utf-8")
        return super().__call__(argv, **kwargs)


class VerifierActs(sp.FakeRun):
    """Verification is the actor here: it may revert, write its own file, and fail on demand."""

    reverts = False          # restore add.py to its base content
    creates = None           # extra path the verification command writes itself
    fail_first = 0           # how many verification rounds must fail before one passes

    def __init__(self, reverts=False, creates=None, fail_first=0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reverts, self.creates, self.fail_first = reverts, creates, fail_first
        self.verifications = 0

    def __call__(self, argv, **kwargs):
        argv = [str(item) for item in argv]
        verification = not (argv[0] == lw.apple.SANDBOX_EXEC
                            or argv == list(lw.FULL_HOST_PROBE_ARGV)
                            or argv[1:2] in (["exec"], ["sandbox"]))
        if not verification:
            return super().__call__(argv, **kwargs)
        self.verifications += 1
        cwd = Path(kwargs["cwd"])
        if self.reverts:
            Path(cwd, "add.py").write_text(sp.BASE_FILES["add.py"], encoding="utf-8")
        if self.creates:
            Path(cwd, self.creates).write_text("written by verification\n", encoding="utf-8")
        outcome = super().__call__(argv, **kwargs)
        outcome["returncode"] = 1 if self.verifications <= self.fail_first else self.test_rc
        outcome["stdout"] = (b"Ran 1 test OK\n" if outcome["returncode"] == 0
                             else b"FAILED (failures=1)\n")
        return outcome


class StatusDecision(unittest.TestCase):
    def test_no_changes_has_its_own_exit_code(self):
        self.assertEqual(lw.EXIT_CODES["no_changes"], 10)
        self.assertEqual(lw.EXIT_CODES["ok"], 0)
        self.assertEqual(frozenset(lw.EXIT_CODES), wl.TERMINAL_STATUSES)
        self.assertIn("no_changes", wl.TERMINAL_STATUSES)   # archivable terminal status

    def test_clean_run_without_a_worker_patch_is_no_changes(self):
        self.assertEqual(lw.decide_status(report(), True), "no_changes")

    def test_no_changes_states_a_concrete_reason(self):
        reason = lw.describe_failure(report(status="no_changes"))
        self.assertIn("no in-scope implementation patch", reason)
        self.assertNotIn("None", reason)

    def test_a_worker_patch_is_still_success(self):
        self.assertEqual(lw.decide_status(report(worker_patch_present=True), True), "ok")

    def test_legacy_reports_without_the_field_stay_compatible(self):
        legacy = report()
        del legacy["worker_patch_present"]
        self.assertEqual(lw.decide_status(legacy, True), "ok")

    def test_failures_keep_priority_over_no_changes(self):
        cases = [
            ({"tests": FAILED}, True, "tests_failed"),
            ({"tests": BLOCKED}, True, "verification_blocked"),
            ({"tests": []}, True, "verification_blocked"),
            ({"primary_unchanged": False}, True, "audit_violation"),
            ({"violations": [{"path": "notes.md", "reason": "outside allowed_paths"}]}, True,
             "audit_violation"),
            ({"attempts": [dict(ATTEMPT, model_routing="mismatch", observed_models=["gpt"])]},
             True, "model_mismatch"),
            ({"attempts": [dict(ATTEMPT, unsupported_tools=True)]}, True, "worker_failed"),
            ({"attempts": [dict(ATTEMPT, terminal_failures=["turn.failed"])]}, True,
             "worker_failed"),
            ({"attempts": [dict(ATTEMPT, returncode=1)]}, False, "worker_failed"),
            ({"attempts": [dict(ATTEMPT, timed_out=True, returncode=-9)]}, False, "timeout"),
            ({"budget": {"exceeded": True}, "tests": []}, True, "timeout"),
        ]
        for index, (overrides, worker_ok, expected) in enumerate(cases):
            with self.subTest(case=index):
                merged = dict(ATTEMPT, has_scoped_patch=False)
                overrides = dict(overrides)
                if "attempts" not in overrides:
                    overrides["attempts"] = [merged]
                self.assertEqual(lw.decide_status(report(worker_patch_present=False,
                                                         **overrides), worker_ok), expected)


class ObservedToolEvidence(unittest.TestCase):
    def evidence(self):
        return {"schema_version": 1,
                "servers": {"fixture": {"enabled_tools": ["echo"], "configured": True,
                                        "probed": True, "observed": False}}}

    def test_earlier_call_survives_an_empty_retry(self):
        first = tools.mark_observations(self.evidence(), call("fixture", "echo") + "\n")
        second = tools.mark_observations(first, "no tool was called this time\n")
        self.assertEqual(second["observed_tools"], [{"server": "fixture", "tool": "echo"}])
        self.assertTrue(second["servers"]["fixture"]["observed"])

    def test_repeated_calls_dedup_across_attempts(self):
        evidence = tools.mark_observations(self.evidence(),
                                           "\n".join([call("fixture", "echo"),
                                                      call("fixture", "echo", "item.started")]))
        evidence = tools.mark_observations(evidence, call("fixture", "echo") + "\n")
        self.assertEqual(evidence["observed_tools"], [{"server": "fixture", "tool": "echo"}])

    def test_only_matching_explicit_lifecycle_counts(self):
        unconfigured = tools.mark_observations(self.evidence(), call("other", "echo") + "\n")
        self.assertEqual(unconfigured["observed_tools"], [])
        self.assertFalse(unconfigured["servers"]["fixture"]["observed"])
        disabled = tools.mark_observations(self.evidence(), call("fixture", "secret_tool") + "\n")
        self.assertEqual(disabled["observed_tools"], [])
        quoted = tools.mark_observations(self.evidence(),
                                         json.dumps({"type": "agent_message",
                                                     "message": "I called fixture echo"}) + "\n")
        self.assertEqual(quoted["observed_tools"], [])
        carried = tools.mark_observations(tools.mark_observations(self.evidence(),
                                                                 call("fixture", "echo")),
                                          call("other", "echo") + "\n")
        self.assertEqual(carried["observed_tools"], [{"server": "fixture", "tool": "echo"}])


class RunJobOutcome(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.options = sp.job_options(self.root, self.repo)

    def run_job(self, fake, options=None):
        with mock.patch.object(lw, "run_bounded", fake):
            return lw.run_job(options or self.options)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX, encoding="utf-8")

    def task_options(self, tag, allowed=("add.py",), max_attempts=1):
        """Options for a contract that also allows the file verification itself writes."""
        settings = sp.write_settings(self.root / ("omlx-" + tag) / "settings.json")
        task = lw.Task.from_payload(sp.task_payload(self.repo, allowed_paths=list(allowed),
                                                    max_attempts=max_attempts))
        return lw.JobOptions(task=task, state_dir=self.root / ("state-" + tag),
                             codex_bin="codex-mock", settings_path=settings,
                             omlx_fetch=sp.FakeOmlx("one"), verify=True)

    def test_worker_patch_that_verification_reverts_is_no_changes(self):
        report = self.run_job(VerifierActs(reverts=True, worker_edit=self.fix))
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(report["changed_paths"], [])
        self.assertEqual(report["patch_bytes"], 0)
        self.assertTrue(report["attempts"][-1]["has_scoped_patch"])   # the worker really edited

    def test_worker_patch_replaced_by_a_different_verifier_file_is_no_changes(self):
        """The worker really patched; verification replaced it with its own artifact."""
        options = self.task_options("replaced", allowed=("add.py", "verify-note.md"))
        report = self.run_job(VerifierActs(reverts=True, creates="verify-note.md",
                                           worker_edit=self.fix), options)
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(report["changed_paths"], ["verify-note.md"])
        self.assertTrue(report["attempts"][0]["has_scoped_patch"])   # patched before the no-op
        self.assertIn("no in-scope implementation patch", report["failure_reason"])

    def test_worker_patch_surviving_beside_a_verifier_file_is_success(self):
        options = self.task_options("survives", allowed=("add.py", "verify-note.md"))
        report = self.run_job(VerifierActs(creates="verify-note.md", worker_edit=self.fix),
                              options)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["worker_patch_present"])
        self.assertEqual(report["changed_paths"], ["add.py", "verify-note.md"])

    def test_retry_with_no_worker_patch_and_a_prior_verifier_artifact_is_no_changes(self):
        options = self.task_options("artifact", allowed=("add.py", "verify-note.md"),
                                    max_attempts=2)
        report = self.run_job(VerifierActs(creates="verify-note.md", fail_first=1), options)
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(report["changed_paths"], ["verify-note.md"])   # survives the retry
        self.assertTrue(all(not attempt["has_scoped_patch"] for attempt in report["attempts"]))

    def test_reverted_worker_patch_becomes_a_verifier_artifact_on_retry(self):
        """A historical worker edit must not launder a leftover verifier file into success."""
        options = self.task_options("laundered", allowed=("add.py", "verify-note.md"),
                                    max_attempts=2)
        report = self.run_job(VerifierActs(reverts=True, creates="verify-note.md", fail_first=1,
                                           worker_edit=self.fix), options)
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertTrue(report["attempts"][0]["has_scoped_patch"])
        self.assertEqual(report["changed_paths"], ["verify-note.md"])
        self.assertNotIn("_worker_authored", report)

    def test_provenance_fingerprints_never_reach_the_report(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix))
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("_worker_authored", report)
        written = json.loads(Path(report["report_dir"], "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("_worker_authored", written)

    def test_no_patch_with_passing_baseline_is_no_changes_exit_10(self):
        report = self.run_job(sp.FakeRun())
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(report["patch_bytes"], 0)
        self.assertIn("no in-scope implementation patch", report["failure_reason"])
        self.assertTrue(all(not attempt["has_scoped_patch"] for attempt in report["attempts"]))

    def test_files_created_only_by_verification_are_not_a_patch(self):
        report = self.run_job(VerifierWrites())
        self.assertEqual(report["status"], "no_changes", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 10)
        self.assertFalse(report["worker_patch_present"])
        self.assertEqual(report["changed_paths"], ["add.py"])   # verifier wrote it
        self.assertGreater(report["patch_bytes"], 0)

    def test_a_real_worker_patch_is_success(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["worker_patch_present"])
        self.assertTrue(report["attempts"][-1]["has_scoped_patch"])

    def test_failed_tests_and_worker_failures_keep_priority(self):
        report = self.run_job(sp.FakeRun(test_rc=1))
        self.assertEqual(report["status"], "tests_failed")
        self.assertEqual(report["exit_code"], 6)
        self.assertFalse(report["worker_patch_present"])
        report = self.run_job(sp.FakeRun(worker_rc=1))
        self.assertEqual(report["status"], "worker_failed")
        self.assertEqual(report["exit_code"], 4)

    def test_second_attempt_patch_counts_even_when_the_last_attempt_is_idle(self):
        def edit_once(cwd, attempt):
            if attempt == 1:
                self.fix(cwd, attempt)
        options = sp.job_options(self.root / "twice", self.repo, max_attempts=2)
        report = self.run_job(sp.FakeRun(test_rc=1, worker_edit=edit_once), options)
        self.assertEqual(report["status"], "tests_failed")
        self.assertTrue(report["worker_patch_present"])


if __name__ == "__main__":
    unittest.main()
