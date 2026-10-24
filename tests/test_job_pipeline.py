"""Audit and fingerprint on real repos, process control, status rules, and a mocked job run."""
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
PASSED = [{"status": "passed", "command": ["python3"], "sandboxed": True}]
BLOCKED = [{"status": "blocked", "command": ["python3"], "returncode": None, "reason": "no sandbox"}]
FAILED = [{"status": "failed", "command": ["python3"], "returncode": 1}]
ATTEMPT = {"attempt": 1, "returncode": 0, "timed_out": False, "model_routing": "ok",
           "unsupported_tools": False, "observed_models": [sp.MODEL]}


def report(**overrides):
    base = {"status": "worker_failed", "attempts": [dict(ATTEMPT)], "tests": PASSED,
            "violations": [], "primary_unchanged": True, "api_model": sp.MODEL,
            "budget": {"exceeded": False}}
    base.update(overrides)
    return base


class Audit(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.base = lw.rev_parse(self.repo, "main")

    def audit(self, allowed=("add.py", "pkg")):
        return lw.audit_worktree(self.repo, self.base, list(allowed), [sp.SECRET],
                                 scratch_dir=self.root / "scratch")

    def test_every_kind_of_uncommitted_change_is_detected(self):
        (self.repo / "add.py").write_text(FIX)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "new.py").write_text("NEW = 1\n")
        audit = self.audit()
        self.assertEqual(audit.changed_paths, ["add.py", "pkg/new.py"])
        self.assertEqual(audit.violations, [])
        self.assertIn("+    return a + b", audit.patch.decode())
        self.assertIn("add.py", audit.diffstat)
        self.assertFalse(lw._inside(lw.audit_scratch_dir(self.repo, self.repo), self.repo))
        (self.repo / "notes.md").write_text("staged\n")
        (self.repo / "extra.txt").write_text("untracked\n")
        sp.git(self.repo, "add", "notes.md")
        audit = self.audit()
        self.assertEqual([item["path"] for item in audit.violations],
                         ["extra.txt", "notes.md"])          # add.py and pkg/new.py stay allowed
        self.assertTrue(all(item["reason"] == "outside allowed_paths" for item in audit.violations))

    def test_rename_reports_both_sides(self):
        sp.git(self.repo, "mv", "notes.md", "moved.md")
        audit = self.audit(["moved.md"])
        self.assertEqual(audit.changed_paths, ["moved.md", "notes.md"])
        roles = {entry["path"]: entry.get("role") for entry in audit.entries if entry.get("role")}
        self.assertEqual(roles, {"notes.md": "rename-source", "moved.md": "rename-destination"})
        self.assertEqual([item["path"] for item in audit.violations], ["notes.md"])

    def test_worker_commit_is_reported_and_preserved_never_reset(self):
        (self.repo / "notes.md").write_text("committed by the worker\n")
        sp.git(self.repo, "add", "-A")
        sp.git(self.repo, "commit", "-q", "-m", "worker commit")
        audit = self.audit()
        self.assertTrue(audit.head_changed)
        self.assertEqual(audit.head_commit, lw.rev_parse(self.repo, "HEAD"))
        self.assertNotEqual(audit.head_commit, self.base)
        self.assertEqual(lw.changed_paths(self.repo), [])       # the tree looks clean ...
        self.assertEqual(audit.changed_paths, ["notes.md"])     # ... the audit still sees it
        reasons = " ".join(item["reason"] for item in audit.violations)
        self.assertIn("moved HEAD", reasons)
        self.assertIn("outside allowed_paths", reasons)

    def test_ignored_output_and_symlink_escapes(self):
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "add.pyc").write_text("junk\n")
        audit = self.audit()
        self.assertEqual(audit.ignored_paths, ["__pycache__"])   # git collapses the whole dir
        self.assertEqual((audit.changed_paths, audit.violations), ([], []))
        (self.root / "outside").mkdir()
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "escape").symlink_to(self.root / "outside")
        audit = self.audit()
        self.assertEqual([item["path"] for item in audit.violations], ["pkg/escape"])
        self.assertIn("symlink escape", audit.violations[0]["reason"])

    def test_primary_fingerprint_compares_dirty_contents_not_only_names(self):
        clean = lw.primary_fingerprint(self.repo)
        self.assertEqual((clean["entries"], clean["head"]), ([], lw.rev_parse(self.repo, "main")))
        self.assertEqual(clean, lw.primary_fingerprint(self.repo))
        (self.repo / "scratch.txt").write_text("dirty before the job\n")
        (self.repo / "add.py").write_text("def add(a, b):\n    return a - b  # local\n")
        before = lw.primary_fingerprint(self.repo)
        self.assertEqual(lw.dirty_primary_files(self.repo), ["add.py", "scratch.txt"])
        (self.repo / "scratch.txt").write_text("touched during the job\n")
        (self.repo / "add.py").write_text("def add(a, b):\n    return a * b\n")
        after = lw.primary_fingerprint(self.repo)
        self.assertEqual([e[1] for e in before["entries"]], [e[1] for e in after["entries"]])
        self.assertEqual((before["head"], before["index_digest"]),
                         (after["head"], after["index_digest"]))
        self.assertNotEqual(before, after)                       # contents differ
        self.assertEqual(after["contents"]["scratch.txt"], lw._digest_path(self.repo / "scratch.txt"))


class Processes(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)

    def test_worker_lock_is_exclusive_then_released(self):
        with lw.job_lock(self.root):
            self.assertIn(str(os.getpid()), (self.root / "worker.lock").read_text())
            with self.assertRaises(lw.BlockedError):
                with lw.job_lock(self.root, timeout=0.2):
                    pass
        with lw.job_lock(self.root, timeout=0.5):
            pass

    def test_run_bounded_kills_the_group_even_when_TERM_is_ignored(self):
        outcome = lw.run_bounded(["/bin/sh", "-c", "echo out; echo err 1>&2; exit 3"], cwd=None,
                                 env=dict(os.environ), timeout=10)
        self.assertEqual((outcome["returncode"], outcome["timed_out"]), (3, False))
        self.assertEqual((outcome["stdout"].strip(), outcome["stderr"].strip()), (b"out", b"err"))
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
                with self.subTest(argv=argv[-1][:22]):
                    outcome = lw.run_bounded(argv, cwd=None, env=dict(os.environ), timeout=0.3)
                    self.assertTrue(outcome["timed_out"])
                    self.assertTrue(outcome["termination"]["terminated"])
                    self.assertEqual(outcome["termination"]["force_killed"], forced)
                    self.assertFalse(outcome["termination"]["survivors"])
        self.assertLess(time.monotonic() - started, 6.0)


class StatusRules(unittest.TestCase):
    def test_success_needs_every_signal_and_every_other_case_is_rejected(self):
        self.assertEqual(lw.decide_status(report(), True), "ok")
        mismatch = dict(ATTEMPT, model_routing="mismatch", observed_models=["gpt"])
        unsupported = dict(ATTEMPT, unsupported_tools=True)
        killed = dict(ATTEMPT, returncode=-9, timed_out=True)
        cases = [({"primary_unchanged": False}, True, "audit_violation"),
                 ({"violations": [{"path": "notes.md", "reason": "outside allowed_paths"}]}, True,
                  "audit_violation"),
                 ({"budget": {"exceeded": True}, "tests": []}, True, "timeout"),
                 ({"budget": {"exceeded": True}, "tests": BLOCKED}, True, "timeout"),
                 ({"attempts": [killed], "tests": FAILED}, False, "timeout"),
                 ({"tests": BLOCKED}, True, "verification_blocked"),
                 ({"tests": [{"status": "skipped", "command": ["python3"]}]}, True,
                  "verification_blocked"),
                 ({"tests": []}, True, "verification_blocked"),
                 ({"tests": PASSED + FAILED}, True, "tests_failed"),
                 ({"attempts": [mismatch]}, True, "model_mismatch"),
                 ({"attempts": [unsupported]}, True, "worker_failed"),
                 ({"attempts": [dict(ATTEMPT, returncode=1)]}, False, "worker_failed")]
        for index, (overrides, worker_ok, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(lw.decide_status(report(**overrides), worker_ok), expected)

    def test_every_non_ok_status_explains_itself(self):
        for status in lw.EXIT_CODES:
            for attempts in ([], [dict(ATTEMPT, returncode=1, timed_out=True, elapsed_seconds=1.0,
                                       unsupported_tools=True, model_routing="mismatch",
                                       observed_models=["gpt"], stderr_excerpt="bad")]):
                for tests in ([], BLOCKED, FAILED):
                    reason = lw.describe_failure(report(status=status, attempts=attempts,
                                                        tests=tests, primary_unchanged=False))
                    if status == "ok":
                        self.assertEqual(reason, "")
                        continue
                    self.assertTrue(reason, (status, len(attempts), len(tests)))
                    self.assertNotIn("None", reason)
        self.assertIn("no verification evidence",
                      lw.describe_failure(report(status="timeout", attempts=[], tests=[])))


class RunJob(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        (self.repo / "sentinel.txt").write_text("dirty before the job\n")   # dirty, uncommitted
        self.options = sp.job_options(self.root, self.repo)

    def run_job(self, fake, options=None):
        with mock.patch.object(lw, "run_bounded", fake):
            return lw.run_job(options or self.options)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX)

    def persisted(self, report):
        return Path(report["report_dir"], "report.json").read_text(encoding="utf-8")

    def test_clean_job_is_verified_and_persists_evidence(self):
        report = self.run_job(sp.FakeRun(worker_edit=self.fix, worker_stderr="leak " + sp.SECRET))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["changed_paths"], ["add.py"])
        self.assertEqual(report["token_counts"], {"input_tokens": 11, "output_tokens": 7})
        self.assertTrue(Path(report["worktree"], "add.py").read_text().endswith("a + b\n"))
        self.assertTrue(Path(report["worktree"]).is_dir())        # preserved, never merged
        self.assertEqual(lw.rev_parse(self.repo, "HEAD"), report["base_commit"])
        self.assertIn("+    return a + b", Path(report["patch_path"]).read_text())
        self.assertEqual(report["dirty_primary_files"], ["sentinel.txt"])
        self.assertTrue(report["primary_unchanged"])
        self.assertEqual(report["primary_fingerprint_before"], report["primary_fingerprint_after"])
        self.assertEqual(report["primary_fingerprint_before"]["contents"]["sentinel.txt"],
                         lw._digest_path(self.repo / "sentinel.txt"))
        self.assertNotIn(sp.SECRET, self.persisted(report))
        self.assertIn("[REDACTED]", self.persisted(report))

    def test_touched_primary_or_out_of_scope_edits_are_violations_despite_passing_tests(self):
        def touch_primary(cwd, attempt):
            self.fix(cwd)
            (self.repo / "sentinel.txt").write_text("touched during the job\n")
        report = self.run_job(sp.FakeRun(worker_edit=touch_primary))
        self.assertEqual(report["status"], "audit_violation")
        self.assertFalse(report["primary_unchanged"])
        self.assertNotEqual(report["primary_fingerprint_before"], report["primary_fingerprint_after"])
        self.assertIn("primary checkout changed", report["failure_reason"])
        self.assertEqual([test["status"] for test in report["tests"]], ["passed"])

        def out_of_scope(cwd, attempt):
            self.fix(cwd)
            Path(cwd, "notes.md").write_text("sneaky\n")
        report = self.run_job(sp.FakeRun(worker_edit=out_of_scope))
        self.assertEqual(report["status"], "audit_violation")
        self.assertEqual([item["path"] for item in report["violations"]], ["notes.md"])
        self.assertIn("outside allowed_paths", report["failure_reason"])

    def test_refusals_are_persisted_and_nothing_is_launched(self):
        fake = sp.FakeRun()
        report = self.run_job(fake, sp.job_options(self.root / "pf", self.repo,
                                                   omlx=sp.FakeOmlx("none_loaded")))
        self.assertEqual((report["status"], fake.calls, report["worktree"]),
                         ("preflight_failed", [], None))
        self.assertIn(sp.MODEL, json.loads(self.persisted(report))["failure_reason"])
        options = sp.job_options(self.root / "sd", self.repo)
        options.state_dir = self.repo / "state"
        report = lw.run_job(options)
        self.assertEqual(report["status"], "contract_invalid")
        self.assertIn("outside the repository", report["failure_reason"])
        self.assertFalse((self.repo / "state").exists())
        report = lw.new_report(self.options)
        with self.assertRaises(lw.DeadlineExpired):
            lw._execute(self.options, report, lw.Deadline(0.0))
        self.assertTrue(Path(report["report_dir"]).is_dir())      # created before preflight
        self.assertEqual(report["preflight"], {})
        real_deadline = lw.Deadline
        with mock.patch.object(lw, "Deadline", lambda total: real_deadline(-1.0)):
            report = lw.run_job(self.options)
        self.assertEqual(report["status"], "timeout")
        self.assertIn("budget", report["failure_reason"])
        self.assertTrue(Path(report["report_dir"], "report.json").is_file())

    def test_failing_tests_deadlines_and_blocked_sandbox_are_never_success(self):
        report = self.run_job(sp.FakeRun(test_rc=1, worker_edit=self.fix))
        self.assertEqual(report["status"], "tests_failed")
        self.assertIn("unittest", report["failure_reason"])
        report = self.run_job(sp.FakeRun(worker_timed_out=True, worker_edit=self.fix))
        self.assertEqual(report["status"], "timeout")
        self.assertIn("killed", report["failure_reason"])
        report = self.run_job(sp.FakeRun(probe_ok=False, worker_edit=self.fix))
        self.assertEqual(report["status"], "verification_blocked")
        self.assertEqual([test["sandboxed"] for test in report["tests"]], [False])
        self.assertEqual(len([call for call in report["tests"] if call["sandboxed"]]), 0)

    def test_second_attempt_receives_feedback(self):
        fake = sp.FakeRun(test_rc=1, worker_edit=self.fix)
        report = self.run_job(fake, sp.job_options(self.root / "tw", self.repo, max_attempts=2))
        self.assertEqual(report["status"], "tests_failed")
        self.assertEqual(fake.exec_calls, 2)
        self.assertEqual(len(report["attempts"]), 2)
        self.assertIn("Previous attempt feedback", fake.calls_of("exec")[1]["stdin"])

    def test_local_key_never_enters_the_model_shell_environment(self):
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
