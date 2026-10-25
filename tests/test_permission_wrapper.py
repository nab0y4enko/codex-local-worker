"""The job's Seatbelt deny wrapper around everything that runs model-influenced code.

Every process that executes code the model wrote or chose (the Codex worker, each verification
command, each native step) runs as ``sandbox-exec -f <report_dir>/permissions.sb <command>``,
with the profile generated from the user's permissions list. The mocked runner strips the
wrapper and records ``wrapped``/``raw_argv``, so these tests read what really would have run.
One class drives a real job through the real ``sandbox-exec`` with a stand-in Codex executable.
"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import permissions  # noqa: E402

lw = sp.lw
NESTED_SANDBOX = 71


def real(path):
    return os.path.realpath(str(path))


class WrapperCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.state = self.root / "state"

    def fix(self, cwd, attempt=1):
        sp.fix_add(cwd, attempt)

    def run_clean_job(self, worker_edit=None, fake_run=None, **overrides):
        return sp.run_clean_job(self.root, self.repo, worker_edit=worker_edit,
                                fake_run=fake_run, **overrides)

    def write_permissions(self, **overrides):
        payload = permissions.permissions_payload(permissions.default_permissions())
        payload.update(overrides)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / permissions.PERMISSIONS_FILE).write_text(json.dumps(payload),
                                                               encoding="utf-8")


class WrappedJobs(WrapperCase):
    def test_worker_and_verification_run_inside_the_wrapper(self):
        report, run = self.run_clean_job()
        worker = run.calls_of("exec")[0]
        self.assertTrue(worker["wrapped"])
        self.assertEqual(worker["raw_argv"][:2], [permissions.SANDBOX_EXEC, "-f"])
        self.assertTrue(all(call["wrapped"] for call in run.verification_calls()))
        self.assertTrue(report["permissions"]["deny_list_applied"])

    def test_parent_commit_during_job_is_reported_not_failed(self):
        def edit(cwd, attempt):
            self.fix(cwd); sp.git(self.repo, "commit", "--allow-empty", "-qm", "parent work")
        report, _ = self.run_clean_job(worker_edit=edit)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["primary_changed_during_job"])

    def test_without_sandbox_exec_jobs_run_and_say_so(self):
        with mock.patch.object(permissions, "wrapper_available", return_value=False):
            report, run = self.run_clean_job()
        self.assertFalse(report["permissions"]["deny_list_applied"])
        self.assertFalse(run.calls_of("exec")[0]["wrapped"])

    def test_the_profile_is_written_per_job_and_reported_by_hash(self):
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        record = report["permissions"]
        profile = Path(report["report_dir"]) / sp.DENY_PROFILE_NAME
        self.assertEqual(record["profile"], str(profile))
        self.assertEqual(record["mode"], permissions.MODE_FULL_HOST)
        self.assertEqual(record["profile_sha256"], hashlib.sha256(profile.read_bytes()).hexdigest())
        self.assertIsNone(record["reason"])
        self.assertEqual(record["denials"], [])
        self.assertEqual(lw.PERMISSIONS_PROFILE_NAME, sp.DENY_PROFILE_NAME)
        self.assertEqual(lw.WRAPPER_PROBE_ARGV, sp.WRAPPER_PROBE)
        # The worker is the inner command exactly as the self-check saw it, behind the profile.
        worker = run.calls_of("exec")[0]
        self.assertEqual(worker["raw_argv"][:3], [permissions.SANDBOX_EXEC, "-f", str(profile)])
        self.assertEqual(worker["raw_argv"][3:], worker["argv"])
        # The wrapper was probed, under the same profile, before the worker started.
        kinds = [call.get("kind") for call in run.calls]
        self.assertLess(kinds.index("wrapper_probe"), kinds.index("exec"))

    def test_the_profile_shuts_the_state_directory_but_reopens_this_jobs_own_paths(self):
        report, _ = self.run_clean_job()
        text = Path(report["permissions"]["profile"]).read_text(encoding="utf-8")
        report_dir = Path(report["report_dir"])
        state = real(self.state)
        self.assertIn(f'(deny file-write* (subpath "{state}"))', text)
        reopened = [line for line in text.splitlines() if line.startswith("(allow file-write* ")
                    and "/.git\")" not in line]
        self.assertEqual(len(reopened), 1, text)
        for path in (report["worktree"], report["codex_home"],
                     report_dir / "attempt-1.last-message.txt",
                     report_dir / lw.NATIVE_SCRATCH_NAME, report_dir / lw.SCREENSHOTS_DIR_NAME):
            with self.subTest(path=str(path)):
                self.assertIn(f'(subpath "{real(path)}")', reopened[0])
        for evidence in ("report.json", "progress.json", sp.DENY_PROFILE_NAME,
                         "attempt-1.jsonl", "changes.patch"):
            with self.subTest(evidence=evidence):
                self.assertNotIn(f"{real(report_dir)}/{evidence}", reopened[0])
                self.assertFalse(sp.seatbelt_write_allowed(text, report_dir / evidence))
        # The user's permissions list and its log cannot be rewritten or moved away.
        self.assertIn(f'(literal "{state}/permissions.json")', text.splitlines()[-1])
        self.assertIn(f'(literal "{state}/permissions.log")', text.splitlines()[-1])
        self.assertIn(f'(literal "{state}")', text.splitlines()[-1])

    def test_verification_entries_say_the_deny_list_was_applied(self):
        report, _ = self.run_clean_job()
        (test,) = report["tests"]
        self.assertIs(test["deny_list_applied"], True)
        self.assertIs(test["sandboxed"], False)
        self.assertIn("deny list", test["confinement"])
        self.assertIn("deny list", report["sandbox"]["confinement"])
        self.assertIs(report["sandbox"]["deny_list_applied"], True)

    def test_a_primary_change_without_the_wrapper_is_still_a_violation(self):
        def edit(cwd, attempt):
            self.fix(cwd); sp.git(self.repo, "commit", "--allow-empty", "-qm", "parent work")
        with mock.patch.object(permissions, "wrapper_available", return_value=False):
            report, _ = self.run_clean_job(worker_edit=edit)
        self.assertEqual(report["status"], "audit_violation")
        self.assertTrue(report["primary_changed_during_job"])
        self.assertIn("primary checkout changed", report["failure_reason"])
        self.assertIn("not available", report["permissions"]["reason"])

    def test_a_clean_job_reports_no_primary_change(self):
        report, _ = self.run_clean_job()
        self.assertIs(report["primary_changed_during_job"], False)
        self.assertTrue(report["primary_unchanged"])


class WrapperRefusals(WrapperCase):
    def test_a_malformed_permissions_file_refuses_the_job(self):
        self.state.mkdir(parents=True)
        (self.state / permissions.PERMISSIONS_FILE).write_text("{not json", encoding="utf-8")
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "preflight_failed")
        self.assertIn("permissions.json", report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])
        self.assertIsNone(report["worktree"])
        self.assertFalse(report["permissions"]["deny_list_applied"])
        self.assertEqual((self.state / permissions.PERMISSIONS_FILE).read_text(), "{not json")

    def test_a_deny_entry_that_would_hide_the_repository_refuses_the_job_naming_it(self):
        self.write_permissions(deny_read=["~/.ssh", str(self.repo.parent)])
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "preflight_failed")
        self.assertIn("would hide the repository", report["failure_reason"])
        self.assertIn(str(self.repo.parent), report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])

    def test_a_profile_sandbox_exec_cannot_apply_runs_unwrapped_and_says_why(self):
        fake = sp.FakeRun(worker_edit=self.fix, wrapper_rc=NESTED_SANDBOX,
                          wrapper_stderr="sandbox-exec: sandbox_apply: Operation not permitted\n")
        report, run = self.run_clean_job(fake_run=fake)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        record = report["permissions"]
        self.assertIs(record["deny_list_applied"], False)
        self.assertIn("71", record["reason"])
        self.assertIn("sandbox_apply", record["reason"])
        self.assertEqual(record["denials"], [])  # the probe's own refusal is not a worker denial
        self.assertFalse(run.calls_of("exec")[0]["wrapped"])
        self.assertFalse(any(call["wrapped"] for call in run.verification_calls()))
        self.assertIs(report["tests"][0]["deny_list_applied"], False)

    def test_without_the_profile_applied_a_primary_change_fails_the_job(self):
        def edit(cwd, attempt):
            self.fix(cwd); (self.repo / "notes.md").write_text("parent edit\n")
        fake = sp.FakeRun(worker_edit=edit, wrapper_rc=NESTED_SANDBOX,
                          wrapper_stderr="sandbox-exec: sandbox_apply: Operation not permitted\n")
        report, _ = self.run_clean_job(fake_run=fake)
        self.assertEqual(report["status"], "audit_violation")
        self.assertTrue(report["primary_changed_during_job"])

    def test_any_other_wrapper_probe_failure_blocks_the_job(self):
        fake = sp.FakeRun(worker_edit=self.fix, wrapper_rc=65,
                          wrapper_stderr="sandbox-exec: unbound variable: bogus\n")
        report, run = self.run_clean_job(fake_run=fake)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("deny wrapper", report["failure_reason"])
        self.assertIn("unbound variable", report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])
        self.assertEqual(run.verification_calls(), [])


class DenialStream(sp.FakeRun):
    """A worker whose shell commands hit the wrapper, and a check that does too."""

    def __init__(self, secret, primary, state, **kwargs):
        super().__init__(**kwargs)
        self.secret, self.primary, self.state = secret, primary, state

    def worker_jsonl(self, argv, cwd, attempt):
        events = [
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "command_execution", "command": "cat key",
                "aggregated_output": f"cat: {self.secret}: Operation not permitted\n",
                "exit_code": 1, "status": "failed"}},
            # Model text that merely quotes a denial is never read as one.
            {"type": "item.completed", "item": {
                "id": "item_2", "type": "agent_message",
                "text": f"cat: {self.primary}/quoted.py: Operation not permitted"}},
        ]
        return "".join(json.dumps(event) + "\n" for event in events) + super().worker_jsonl(
            argv, cwd, attempt)

    def verification_output(self, argv, cwd):
        return f"touch: {self.primary}/generated.txt: Operation not permitted\nRan 1 test OK\n"


class Denials(WrapperCase):
    def test_denials_in_worker_commands_and_verification_output_are_reported(self):
        secret = real(os.path.expanduser("~/.ssh")) + "/id_ed25519"
        primary = real(self.repo)
        fake = DenialStream(secret, primary, real(self.state), worker_edit=self.fix,
                            worker_stderr=f"Error: cannot write {real(self.state)}/permissions.json:"
                                          " Operation not permitted\n")
        report, _ = self.run_clean_job(fake_run=fake)
        denials = report["permissions"]["denials"]
        self.assertIn({"path": secret, "kind": "read", "source": "worker attempt 1"}, denials)
        self.assertIn({"path": f"{real(self.state)}/permissions.json", "kind": "write",
                       "source": "worker attempt 1"}, denials)
        self.assertIn({"path": f"{primary}/generated.txt", "kind": "write",
                       "source": "verification attempt 1 command 1"}, denials)
        self.assertNotIn(f"{primary}/quoted.py", json.dumps(denials))
        self.assertEqual(len(denials), 3, denials)

    def test_worker_command_outputs_reads_only_shell_command_items(self):
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "aggregated_output": "one\n"}},
            {"type": "item.started", "item": {"type": "command_execution",
                                              "aggregated_output": "partial\n"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "two"}},
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "aggregated_output": ""}},
        )) + "\nnot json\n"
        self.assertEqual(lw.worker_command_outputs(stream), ["one\n"])


class StatusRules(unittest.TestCase):
    def report(self, applied, **overrides):
        passed = [{"status": "passed", "command": ["python3"], "returncode": 0}]
        report = {"attempts": [{"attempt": 1, "returncode": 0, "model_routing": "match"}],
                  "tests": passed, "violations": [], "primary_unchanged": False,
                  "worker_patch_present": True, "budget": {"exceeded": False},
                  "permissions": {"deny_list_applied": applied}}
        report.update(overrides)
        return report

    def test_a_primary_change_is_informational_only_while_the_deny_list_is_applied(self):
        self.assertEqual(lw.decide_status(self.report(True), True), "ok")
        self.assertEqual(lw.decide_status(self.report(False), True), "audit_violation")
        legacy = self.report(True)
        del legacy["permissions"]
        self.assertEqual(lw.decide_status(legacy, True), "audit_violation")

    def test_describe_failure_names_a_primary_change_only_when_it_counts(self):
        counted = lw.describe_failure(dict(self.report(False), status="audit_violation"))
        self.assertIn("primary checkout changed", counted)
        informational = lw.describe_failure(dict(self.report(True), status="tests_failed",
            tests=[{"status": "failed", "command": ["x"], "returncode": 1}]))
        self.assertNotIn("primary checkout changed", informational)

    def test_the_summary_states_the_wrapper_and_an_unattributed_primary_change(self):
        report = dict(self.report(True), status="ok", exit_code=0,
                      primary_changed_during_job=True)
        report["permissions"] = {"deny_list_applied": True, "denials": [{"path": "/x", "kind": "read"}]}
        text = "\n".join(lw.summary_lines(report))
        self.assertIn("deny list applied", text)
        self.assertIn("changed during job, not attributed", text)
        report["permissions"] = {"deny_list_applied": False, "reason": "sandbox-exec missing",
                                 "denials": []}
        self.assertIn("deny list NOT applied (sandbox-exec missing)",
                      "\n".join(lw.summary_lines(report)))

    def test_the_limitations_describe_the_deny_wrapper_truthfully(self):
        text = " ".join(lw.LIMITATIONS)
        self.assertNotIn("no sandbox confinement", text)
        self.assertIn("deny list", text)
        self.assertIn("deny_list_applied", text)
        self.assertNotIn("no sandbox confines the worker", lw.__doc__)


STUB_CODEX = textwrap.dedent('''\
    import json, os, sys
    argv = sys.argv
    out, cwd, model = (argv[argv.index(flag) + 1] for flag in ("-o", "-C", "-m"))
    sys.stdin.read()
    home = os.environ["CODEX_HOME"]
    report_dir = os.path.dirname(home)
    state = os.path.dirname(os.path.dirname(report_dir))
    results = {}

    def attempt(label, action):
        try:
            action()
            results[label] = "ok"
        except OSError as error:
            results[label] = error.strerror

    def write(path, text="x\\n", mode="w"):
        with open(path, mode) as handle:
            handle.write(text)

    attempt("worktree", lambda: write(os.path.join(cwd, "add.py"),
                                      "def add(a, b):\\n    return a + b\\n"))
    attempt("codex_home", lambda: write(os.path.join(home, "session.log")))
    attempt("permissions_json", lambda: write(os.path.join(state, "permissions.json"), "", "a"))
    attempt("permissions_log", lambda: write(os.path.join(state, "permissions.log"), "{}", "a"))
    attempt("report_json", lambda: write(os.path.join(report_dir, "report.json")))
    attempt("profile", lambda: write(os.path.join(report_dir, "permissions.sb"), "", "a"))
    attempt("other_job", lambda: os.mkdir(os.path.join(state, "worktrees", "planted")))
    attempt("state_moved", lambda: os.rename(state, state + "-moved"))
    write(out, json.dumps(results, sort_keys=True))
    print(json.dumps({"type": "turn.completed", "model": model,
                      "usage": {"input_tokens": 3, "output_tokens": 2}}))
''')


@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
class RealWrappedJob(WrapperCase):
    """One job end to end through the real ``sandbox-exec``: only Codex is a stand-in script."""

    def stub_codex(self):
        script = self.root / "codex-stub.py"
        script.write_text(STUB_CODEX, encoding="utf-8")
        codex = self.root / "codex"
        codex.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
        codex.chmod(0o755)
        return codex

    def test_the_wrapped_worker_writes_its_worktree_but_never_the_launcher_state(self):
        codex = self.stub_codex()
        options = sp.job_options(self.root, self.repo,
                                 test_commands=[[sys.executable, "-c", "print('checked')"]])
        options.codex_bin = str(codex)
        before = permissions.load_permissions(self.state)
        permissions_file = (self.state / permissions.PERMISSIONS_FILE).read_bytes()
        report = lw.run_job(options)
        if not report["permissions"]["deny_list_applied"]:
            self.skipTest("sandbox-exec cannot apply a profile here: "
                          + str(report["permissions"]["reason"]))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        results = json.loads(Path(report["report_dir"], "attempt-1.last-message.txt").read_text())
        self.assertEqual(results["worktree"], "ok")      # its own worktree
        self.assertEqual(results["codex_home"], "ok")    # its own Codex home
        for denied in ("permissions_json", "permissions_log", "report_json", "profile",
                       "other_job", "state_moved"):
            with self.subTest(denied=denied):
                self.assertEqual(results[denied], "Operation not permitted")
        self.assertEqual((self.state / permissions.PERMISSIONS_FILE).read_bytes(), permissions_file)
        self.assertEqual(permissions.load_permissions(self.state), before)
        self.assertFalse((self.state / "worktrees" / "planted").exists())
        self.assertTrue(self.state.is_dir())
        self.assertEqual(report["tests"][0]["status"], "passed")
        self.assertIs(report["tests"][0]["deny_list_applied"], True)


if __name__ == "__main__":
    unittest.main()
