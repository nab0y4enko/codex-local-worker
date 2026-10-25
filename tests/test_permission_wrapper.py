"""The job's Seatbelt deny wrapper around everything that runs model-influenced code.

Every process that executes code the model wrote or chose (the Codex worker, each verification
command, each native step) runs as ``sandbox-exec -f <report_dir>/permissions.sb <command>``,
with the profile generated from the user's permissions list. The mocked runner strips the
wrapper and records ``wrapped``/``raw_argv``, so these tests read what really would have run.
One class drives a real job through the real ``sandbox-exec`` with a stand-in Codex executable.
"""
import errno
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
import codex_catalog  # noqa: E402
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
        lines = text.splitlines()
        self.assertIn(f'(deny file-write* (subpath "{state}"))', lines)
        # The state re-open is the allow statement directly after the state deny.
        state_deny = lines.index(f'(deny file-write* (subpath "{state}"))')
        reopen = lines[state_deny + 1]
        self.assertTrue(reopen.startswith("(allow file-write* "))
        for path in (report["worktree"], report["codex_home"],
                     report_dir / "attempt-1.last-message.txt",
                     report_dir / lw.NATIVE_SCRATCH_NAME, report_dir / lw.SCREENSHOTS_DIR_NAME):
            with self.subTest(path=str(path)):
                self.assertIn(f'(subpath "{real(path)}")', reopen)
        for evidence in ("report.json", "progress.json", sp.DENY_PROFILE_NAME,
                         "worker-attempt-1.jsonl", "changes.patch"):
            with self.subTest(evidence=evidence):
                self.assertNotIn(f"{real(report_dir)}/{evidence}", reopen)
                self.assertFalse(sp.seatbelt_write_allowed(text, report_dir / evidence))
        # The user's permissions list and its log cannot be rewritten or moved away.
        self.assertIn(f'(literal "{state}/permissions.json")', lines[-1])
        self.assertIn(f'(literal "{state}/permissions.log")', lines[-1])
        self.assertIn(f'(literal "{state}")', lines[-1])

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


class CatalogAndLastMessage(WrapperCase):
    """R23: the model catalog is the launcher's file (report dir, not the worker's Codex home),
    and a missing ``-o`` file is a missing file, not tampering."""

    def test_the_catalog_lives_in_the_report_dir_and_not_in_the_workers_codex_home(self):
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        report_dir, codex_home = Path(report["report_dir"]), Path(report["codex_home"])
        catalog = report_dir / codex_catalog.CATALOG_NAME
        self.assertTrue(catalog.is_file())
        self.assertFalse((codex_home / codex_catalog.CATALOG_NAME).exists())
        self.assertEqual(report["catalog_sha256"], hashlib.sha256(catalog.read_bytes()).hexdigest())
        pin = lw.config_overrides_map(run.calls_of("exec")[0]["argv"])[lw.CATALOG_PIN_KEY]
        self.assertEqual(pin, json.dumps(str(catalog)))

    def test_the_profile_does_not_reopen_the_catalog_but_reopens_the_codex_home(self):
        report, _ = self.run_clean_job()
        text = Path(report["permissions"]["profile"]).read_text(encoding="utf-8")
        report_dir = Path(report["report_dir"])
        self.assertFalse(sp.seatbelt_write_allowed(text, report_dir / codex_catalog.CATALOG_NAME))
        self.assertTrue(sp.seatbelt_write_allowed(text, Path(report["codex_home"]) / "session.log"))

    def test_a_changed_catalog_is_still_refused_by_the_self_check(self):
        profile = sp.discovered_profile()
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        path = root / codex_catalog.CATALOG_NAME
        sha = codex_catalog.write_catalog(path, codex_catalog.build_local_entry(profile.model, 32768))
        argv = lw.worker_argv("codex", root, profile, 32768, root / "o.txt", catalog_path=path)
        self.assertEqual(lw.missing_config_pins(argv, profile, 32768, (path, sha)), [])
        path.write_text("{}\n")
        (problem,) = lw.missing_config_pins(argv, profile, 32768, (path, sha))
        self.assertIn("changed after it was generated", problem)

    def test_a_catalog_swapped_for_a_symlink_is_refused_without_reading_through_it(self):
        profile = sp.discovered_profile()
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        path = root / codex_catalog.CATALOG_NAME
        sha = codex_catalog.write_catalog(path, codex_catalog.build_local_entry(profile.model, 32768))
        secret = root / "secret.txt"
        secret.write_text("not for the launcher\n")
        path.unlink()
        path.symlink_to(secret)
        argv = lw.worker_argv("codex", root, profile, 32768, root / "o.txt", catalog_path=path)
        (problem,) = lw.missing_config_pins(argv, profile, 32768, (path, sha))
        self.assertIn("can no longer be read", problem)
        self.assertNotIn(hashlib.sha256(secret.read_bytes()).hexdigest(), problem)

    def test_a_missing_last_message_file_is_recorded_as_missing(self):
        report, _ = self.run_clean_job()  # the mocked worker never writes its -o file
        (attempt,) = report["attempts"]
        self.assertIn("No such file", attempt["last_message_missing"])
        self.assertNotIn("last_message_tampered", attempt)
        self.assertEqual(attempt["last_message_excerpt"], "")

    def test_a_symlink_at_the_last_message_path_is_tampering_and_never_read(self):
        secret = self.root / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT\n")

        def edit(cwd, attempt):
            self.fix(cwd)
            link = Path(next(iter(self.report_dirs()))) / "attempt-1.last-message.txt"
            link.symlink_to(secret)

        report, _ = self.run_clean_job(worker_edit=edit)
        (attempt,) = report["attempts"]
        self.assertIn("symlink", attempt["last_message_tampered"])
        self.assertNotIn("last_message_missing", attempt)
        self.assertNotIn("TOP-SECRET", json.dumps(report))

    def report_dirs(self):
        return sorted((self.state / "reports").iterdir())


class NativeRoots(WrapperCase):
    """NB1: the roots the profile re-opens and rename-protects are made by the launcher."""

    def test_both_roots_are_created_private_and_reused_when_present(self):
        report_dir = self.root / "report"
        report_dir.mkdir()
        roots = lw.create_native_roots(report_dir)
        self.assertEqual([root.name for root in roots], [lw.NATIVE_SCRATCH_NAME, lw.SCREENSHOTS_DIR_NAME])
        for root in roots:
            self.assertTrue(root.is_dir())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        (roots[0] / "keep").write_text("x")
        lw.create_native_roots(report_dir)  # idempotent: nothing is replaced
        self.assertTrue((roots[0] / "keep").exists())

    def test_a_link_or_file_in_the_way_is_refused(self):
        report_dir = self.root / "report"
        report_dir.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (report_dir / lw.NATIVE_SCRATCH_NAME).symlink_to(elsewhere)
        with self.assertRaises(lw.BlockedError):
            lw.create_native_roots(report_dir)
        (report_dir / lw.NATIVE_SCRATCH_NAME).unlink()
        (report_dir / lw.NATIVE_SCRATCH_NAME).write_text("a file")
        with self.assertRaises(lw.BlockedError):
            lw.create_native_roots(report_dir)

    def test_a_job_creates_them_before_the_profile_is_built(self):
        report, _ = self.run_clean_job()
        report_dir = Path(report["report_dir"])
        self.assertTrue((report_dir / lw.NATIVE_SCRATCH_NAME).is_dir())
        self.assertTrue((report_dir / lw.SCREENSHOTS_DIR_NAME).is_dir())
        text = Path(report["permissions"]["profile"]).read_text(encoding="utf-8")
        self.assertTrue(sp.seatbelt_write_allowed(text, report_dir / lw.NATIVE_SCRATCH_NAME / "derived" / "x"))
        self.assertTrue(sp.seatbelt_write_allowed(text, report_dir / lw.SCREENSHOTS_DIR_NAME / "a.png"))


class DenyWriteConflicts(WrapperCase):
    def test_a_deny_write_entry_covering_the_state_directory_refuses_the_job(self):
        self.write_permissions(deny_write=[str(self.state)])
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "preflight_failed")
        self.assertIn("deny_write entry", report["failure_reason"])
        self.assertIn("allow-write", report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])
        self.assertIsNone(report["worktree"])

    def test_a_deny_write_entry_covering_the_report_directory_refuses_the_job(self):
        # The job id (so the report dir) is not known until the job starts: an entry above every
        # report dir is the one a user can write, and it is caught like the state directory.
        self.write_permissions(deny_write=[str(self.state / "reports")])
        report, run = self.run_clean_job()
        self.assertEqual(report["status"], "preflight_failed")
        self.assertIn("report directory", report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])

    def test_the_default_deny_write_list_refuses_nothing(self):
        report, _ = self.run_clean_job()
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))



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
        self.assertIn({"path": secret, "kind": "read", "cause": "deny_read",
                       "source": "worker attempt 1"}, denials)
        self.assertIn({"path": f"{real(self.state)}/permissions.json", "kind": "write",
                       "cause": "state_dir", "source": "worker attempt 1"}, denials)
        self.assertIn({"path": f"{primary}/generated.txt", "kind": "write",
                       "cause": "primary_checkout",
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

    def write_nested(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return write(path)

    attempt("worktree", lambda: write(os.path.join(cwd, "add.py"),
                                      "def add(a, b):\\n    return a + b\\n"))
    attempt("codex_home", lambda: write(os.path.join(home, "session.log")))
    attempt("permissions_json", lambda: write(os.path.join(state, "permissions.json"), "", "a"))
    attempt("permissions_log", lambda: write(os.path.join(state, "permissions.log"), "{}", "a"))
    attempt("report_json", lambda: write(os.path.join(report_dir, "report.json")))
    attempt("profile", lambda: write(os.path.join(report_dir, "permissions.sb"), "", "a"))
    attempt("other_job", lambda: os.mkdir(os.path.join(state, "worktrees", "planted")))
    attempt("state_moved", lambda: os.rename(state, state + "-moved"))
    # R23: the catalog is the launcher's file, not the worker's.
    attempt("catalog", lambda: write(os.path.join(report_dir, "local-model-catalog.json"), "{}", "a"))
    attempt("catalog_replaced", lambda: (os.unlink(os.path.join(report_dir, "local-model-catalog.json")),))
    # NB1: native tools create their own directories under the two pre-made roots.
    attempt("scratch_mkdir", lambda: os.makedirs(os.path.join(report_dir, "apple-scratch", "derived", "Build")))
    attempt("scratch_write", lambda: write(os.path.join(report_dir, "apple-scratch", "derived", "Build", "f")))
    attempt("capture_native_write", lambda: write_nested(os.path.join(report_dir, "apple-scratch", "capture-native", "source", "apple-scratch", "products", "build.log")))
    attempt("capture_checkout_write", lambda: write_nested(os.path.join(report_dir, "apple-scratch", "capture-worktrees", "source", ".git", "config")))
    attempt("screenshot", lambda: write(os.path.join(report_dir, "screenshots", "shot.png")))
    attempt("scratch_root_moved", lambda: os.rename(os.path.join(report_dir, "apple-scratch"),
                                                    os.path.join(report_dir, "scratch-moved")))
    # R22: the resolved common dir of the primary, wherever it is (read from the gitfile).
    admin = open(os.path.join(cwd, ".git")).read().split("gitdir:", 1)[1].strip()
    common = os.path.realpath(os.path.join(admin, open(os.path.join(admin, "commondir")).read().strip()))
    attempt("common_config", lambda: write(os.path.join(common, "config"), "[core]\\n", "a"))
    attempt("common_attributes", lambda: write(os.path.join(common, "info", "attributes"), "* filter=x\\n"))
    attempt("common_merge_head", lambda: write(os.path.join(common, "MERGE_HEAD"), "0" * 40))
    attempt("common_objects", lambda: write(os.path.join(common, "objects", "zz-job-object")))
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

    def run_stub_job(self, repo):
        codex = self.stub_codex()
        options = sp.job_options(self.root, repo, test_commands=[[sys.executable, "-c", "print('checked')"]])
        options.codex_bin = str(codex)
        report = lw.run_job(options)
        if not report["permissions"]["deny_list_applied"]:
            self.skipTest("sandbox-exec cannot apply a profile here: " + str(report["permissions"]["reason"]))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        return report, json.loads(Path(report["report_dir"], "attempt-1.last-message.txt").read_text())

    def test_the_catalog_is_read_only_and_the_native_roots_are_creatable_in_a_real_job(self):
        report, results = self.run_stub_job(self.repo)
        # R23: the worker can neither rewrite nor replace the launcher's catalog.
        self.assertEqual(results["catalog"], "Operation not permitted")
        self.assertEqual(results["catalog_replaced"], "Operation not permitted")
        catalog = Path(report["report_dir"]) / codex_catalog.CATALOG_NAME
        self.assertEqual(report["catalog_sha256"], hashlib.sha256(catalog.read_bytes()).hexdigest())
        # NB1: apple-scratch and screenshots are pre-created, so tools can build under them, but
        # the roots themselves cannot be moved away.
        for name in ("scratch_mkdir", "scratch_write", "capture_native_write",
                     "capture_checkout_write", "screenshot"):
            with self.subTest(name=name):
                self.assertEqual(results[name], "ok")
        self.assertEqual(results["scratch_root_moved"], "Operation not permitted")
        self.assertTrue((Path(report["report_dir"]) / lw.NATIVE_SCRATCH_NAME / "derived" / "Build" / "f").is_file())
        self.assertTrue((Path(report["report_dir"]) / lw.NATIVE_SCRATCH_NAME / "capture-native" / "source" /
                         "apple-scratch" / "products" / "build.log").is_file())
        self.assertTrue((Path(report["report_dir"]) / lw.NATIVE_SCRATCH_NAME / "capture-worktrees" /
                         "source" / ".git" / "config").is_file())
        self.assertTrue((Path(report["report_dir"]) / lw.SCREENSHOTS_DIR_NAME / "shot.png").is_file())

    def test_a_job_on_a_primary_with_a_separate_git_dir_cannot_write_its_common_dir(self):
        primary = self.root / "sep-primary"
        sep = self.root / "gitdirs" / "sep.git"
        sep.parent.mkdir()
        sp.git(self.root, "init", "-q", "--initial-branch=main", "--separate-git-dir", str(sep), str(primary))
        for name, text in sp.BASE_FILES.items():
            (primary / name).write_text(text)
        sp.git(primary, "add", "-A")
        sp.git(primary, "commit", "-q", "-m", "base")
        config_before = (sep / "config").read_bytes()
        report, results = self.run_stub_job(primary)
        for name in ("common_config", "common_attributes", "common_merge_head"):
            with self.subTest(name=name):
                self.assertEqual(results[name], "Operation not permitted")
        self.assertEqual(results["common_objects"], "ok")  # objects/ stays writable for commits
        self.assertEqual((sep / "config").read_bytes(), config_before)
        self.assertFalse((sep / "info" / "attributes").exists())
        self.assertFalse((sep / "MERGE_HEAD").exists())

    def test_a_job_on_a_plain_primary_cannot_write_its_common_dir_either(self):
        report, results = self.run_stub_job(self.repo)
        for name in ("common_config", "common_attributes", "common_merge_head"):
            with self.subTest(name=name):
                self.assertEqual(results[name], "Operation not permitted")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())


class JobWritablePaths(WrapperCase):
    def test_a_tmpdir_that_covers_the_report_directory_is_not_reopened(self):
        report_dir = self.state / "reports" / "job-1"
        report_dir.mkdir(parents=True)
        worktree = self.state / "worktrees" / "job-1"
        codex_home = report_dir / "codex-home"
        # TMPDIR is the report directory itself: re-opening it would re-open the report directory.
        paths = lw.job_writable_paths(report_dir, worktree, codex_home, 1, {"TMPDIR": str(report_dir)})
        self.assertNotIn(Path(str(report_dir)), paths)
        self.assertIn(worktree, paths)
        # A TMPDIR elsewhere is kept.
        elsewhere = self.state / "tmp"
        kept = lw.job_writable_paths(report_dir, worktree, codex_home, 1, {"TMPDIR": str(elsewhere)})
        self.assertIn(elsewhere, kept)


FIFO_PROBE = textwrap.dedent('''\
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import local_worker as lw
    from pathlib import Path
    path, report_dir = Path(sys.argv[2]), Path(sys.argv[3])
    print(json.dumps({"read": list(lw.safe_read_job_text(path, report_dir, [])),
                      "sha": lw.job_file_sha256(path)}))
''')


class SafeJobRead(WrapperCase):
    """R23: one ``O_NOFOLLOW|O_NONBLOCK`` regular-file read serves every job file the launcher
    reads after the worker ran."""

    def setUp(self):
        super().setUp()
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()

    def test_a_regular_file_inside_the_report_dir_is_read(self):
        target = self.report_dir / "attempt-1.last-message.txt"
        target.write_text("done\n")
        self.assertEqual(lw.safe_read_job_text(target, self.report_dir, []), ("done\n", None, None))

    def test_a_symlink_in_place_of_the_file_is_not_followed(self):
        secret = self.root / "secret"
        secret.write_text("SECRET\n")
        target = self.report_dir / "attempt-1.last-message.txt"
        target.symlink_to(secret)
        text, missing, tampered = lw.safe_read_job_text(target, self.report_dir, [])
        self.assertEqual(text, "")
        self.assertIsNone(missing)
        self.assertIn("symlink", tampered)
        self.assertNotIn("SECRET", text)

    def test_a_file_whose_real_path_leaves_the_report_dir_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link_dir = self.report_dir / "sub"
        link_dir.symlink_to(outside)
        (outside / "f").write_text("SECRET\n")
        text, missing, tampered = lw.safe_read_job_text(link_dir / "f", self.report_dir, [])
        self.assertEqual(text, "")
        self.assertIsNone(missing)
        self.assertIsNotNone(tampered)

    def test_a_missing_file_is_missing_not_tampered(self):
        text, missing, tampered = lw.safe_read_job_text(self.report_dir / "nope", self.report_dir, [])
        self.assertEqual(text, "")
        self.assertIn("No such file", missing)
        self.assertIsNone(tampered)

    def test_a_symlink_to_a_sibling_inside_the_report_dir_is_still_not_followed(self):
        # O_NOFOLLOW refuses even a symlink whose target is inside the report dir (so the realpath
        # check alone would pass): evidence is never read through a swap.
        sibling = self.report_dir / "real.txt"
        sibling.write_text("sibling\n")
        target = self.report_dir / "attempt-1.last-message.txt"
        target.symlink_to(sibling)
        text, missing, tampered = lw.safe_read_job_text(target, self.report_dir, [])
        self.assertEqual(text, "")
        self.assertIsNone(missing)
        self.assertIsNotNone(tampered)

    def probe(self, path):
        """Run the readers on ``path`` in a child that is killed after 15s: a hang is a failure."""
        outcome = subprocess.run([sys.executable, "-I", "-c", FIFO_PROBE, str(Path(lw.__file__).parent),
                                  str(path), str(self.report_dir)],
                                 capture_output=True, timeout=15)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        return json.loads(outcome.stdout)

    def test_a_fifo_at_the_path_is_refused_without_hanging(self):
        fifo = self.report_dir / "attempt-1.last-message.txt"
        os.mkfifo(str(fifo))
        found = self.probe(fifo)
        self.assertEqual(found["read"][0], "")
        self.assertIsNone(found["read"][1])
        self.assertIn("not a regular file", found["read"][2])
        self.assertIsNone(found["sha"])

    def test_a_symlink_to_a_fifo_is_refused_without_hanging(self):
        fifo = self.report_dir / "pipe"
        os.mkfifo(str(fifo))
        link = self.report_dir / "attempt-1.last-message.txt"
        link.symlink_to(fifo)
        found = self.probe(link)
        self.assertEqual(found["read"][0], "")
        self.assertIn("symlink", found["read"][2])
        self.assertIsNone(found["sha"])

    def test_the_hash_helper_never_reads_through_a_link(self):
        secret = self.root / "secret"
        secret.write_text("SECRET\n")
        plain = self.report_dir / "catalog.json"
        plain.write_text("{}\n")
        link = self.report_dir / "link.json"
        link.symlink_to(secret)
        self.assertEqual(lw.job_file_sha256(plain), hashlib.sha256(b"{}\n").hexdigest())
        self.assertIsNone(lw.job_file_sha256(link))
        self.assertIsNone(lw.job_file_sha256(self.report_dir / "absent.json"))

    def test_read_regular_file_names_what_was_wrong(self):
        # (A FIFO is covered above in a child process with a timeout: opening one here with a
        # regression in the helper would hang the whole run instead of failing it.)
        with self.assertRaises(lw.NotRegularFile):
            lw.read_regular_file(self.report_dir)  # a directory
        target = self.report_dir / "real.txt"
        target.write_text("x")
        self.assertEqual(lw.read_regular_file(target), b"x")
        link = self.report_dir / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError) as caught:
            lw.read_regular_file(link)
        self.assertIn(caught.exception.errno, (errno.ELOOP, errno.EMLINK))

    def test_the_digest_of_a_path_state_does_not_block_on_a_fifo(self):
        fifo = self.report_dir / "pipe"
        os.mkfifo(str(fifo))
        outcome = subprocess.run([sys.executable, "-I", "-c",
                                  "import sys; sys.path.insert(0, sys.argv[1]); import local_worker as lw; "
                                  "from pathlib import Path; print(lw.path_state(Path(sys.argv[2])))",
                                  str(Path(lw.__file__).parent), str(fifo)], capture_output=True, timeout=15)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertIn("unreadable:NotRegularFile", outcome.stdout.decode())


class UnwrappedAdminTamper(WrapperCase):
    """Item 1(b): when the deny list is not applied the worker CAN repoint the gitfile, but the
    launcher pins its own git layout, so the audit is not steered, reads the real HEAD, and flags
    the change as a violation."""

    def build_attacker(self, sentinel):
        attacker = self.root / "attacker"
        sp.git(self.root, "init", "-q", str(attacker))
        (attacker / "x.txt").write_text("a\n")
        sp.git(attacker, "add", "x.txt")
        sp.git(attacker, "commit", "-qm", "attacker commit")  # a DIFFERENT HEAD than the job base
        gitdir = attacker / ".git"
        sp.git(attacker, "config", "filter.pwn.clean", f"sh -c 'echo tampered > {sentinel}; cat'")
        (gitdir / "info").mkdir(exist_ok=True)
        (gitdir / "info" / "attributes").write_text("* filter=pwn\n")
        (gitdir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (gitdir / "objects" / "info" / "alternates").write_text(
            str(self.repo / ".git" / "objects") + "\n")
        return gitdir

    def test_a_repointed_gitfile_in_an_unwrapped_job_is_caught_not_followed(self):
        sentinel = self.root / "sentinel"

        def edit(cwd, attempt):
            self.fix(cwd)
            gitdir = self.build_attacker(sentinel)
            (Path(cwd) / ".git").write_text(f"gitdir: {gitdir}\n")

        with mock.patch.object(permissions, "wrapper_available", return_value=False):
            report, run = self.run_clean_job(worker_edit=edit)
        self.assertFalse(report["permissions"]["deny_list_applied"])
        self.assertFalse(sentinel.exists(), "the attacker filter ran in the launcher's git")
        self.assertEqual(report["git_admin_tampered"], ["gitfile"])
        self.assertEqual(report["status"], "audit_violation")
        # R24: once a redirect file differs the audit runs no git at all, so no HEAD is read from
        # the repointed repository and none is reported.
        self.assertEqual(report["head_commit"], "")
        self.assertTrue(any(v["path"] == ".git" for v in report["violations"]))

    def test_a_planted_config_worktree_is_caught_before_git_runs_it(self):
        """R24: with ``extensions.worktreeConfig`` pinning the layout does not stop a planted
        ``<admin>/config.worktree``, so the audit compares the redirect fingerprint before every
        git call and skips git (recording the violation) when it differs."""
        sentinel = self.root / "sentinel"
        sp.git(self.repo, "config", "extensions.worktreeConfig", "true")

        def edit(cwd, attempt):
            self.fix(cwd)
            admin = Path(sp.git(cwd, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
            sp.git(cwd, "config", "--file", str(admin / "config.worktree"), "filter.pwn.clean",
                   f"sh -c 'echo tampered > {sentinel}; cat'")
            (Path(cwd) / ".gitattributes").write_text("* filter=pwn\n")

        with mock.patch.object(permissions, "wrapper_available", return_value=False):
            report, run = self.run_clean_job(worker_edit=edit)
        self.assertFalse(sentinel.exists(), "the planted config.worktree filter ran in the launcher's git")
        self.assertEqual(report["git_admin_tampered"], ["config.worktree"])
        self.assertEqual(report["status"], "audit_violation")
        (violation,) = [v for v in report["violations"] if v["path"] == ".git"]
        self.assertIn("config.worktree", violation["reason"])

    def test_the_audit_runs_no_git_at_all_once_a_redirect_file_differs(self):
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        layout = lw.worktree_git_layout(worktree)
        before = lw.git_admin_fingerprint(layout, worktree)
        Path(layout["GIT_DIR"], "config.worktree").write_text("[core]\n")
        base = sp.git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
        with mock.patch.object(lw, "git", side_effect=AssertionError("git ran")):
            audit = lw.audit_worktree(worktree, base, ["add.py"], [], scratch_dir=self.root,
                                      git_layout=layout, git_admin_before=before)
        self.assertEqual(audit.patch, b"")
        self.assertEqual(audit.changed_paths, [])
        (violation,) = audit.violations
        self.assertEqual(violation["path"], ".git")
        self.assertIn("git redirect file", violation["reason"])
        self.assertIn("config.worktree", violation["reason"])
        self.assertIn("git was not run", violation["reason"])

    def test_an_unchanged_fingerprint_lets_the_audit_run_normally(self):
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        layout = lw.worktree_git_layout(worktree)
        before = lw.git_admin_fingerprint(layout, worktree)
        (worktree / "add.py").write_text("def add(a, b):\n    return a + b\n")
        base = sp.git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
        audit = lw.audit_worktree(worktree, base, ["add.py"], [], scratch_dir=self.root,
                                  git_layout=layout, git_admin_before=before)
        self.assertEqual(audit.changed_paths, ["add.py"])
        self.assertEqual(audit.violations, [])

    def test_the_redirect_file_names_are_defined_once_in_the_permissions_module(self):
        layout = {"GIT_DIR": str(self.root / "admin")}
        (self.root / "admin").mkdir()
        self.assertEqual(list(lw.git_admin_fingerprint(layout, self.root)),
                         ["gitfile", *permissions.GIT_ADMIN_REDIRECTS])
        source = Path(lw.__file__).read_text(encoding="utf-8")
        self.assertNotIn('GIT_ADMIN_REDIRECTS = ("commondir"', source)
        self.assertIn("permissions.GIT_ADMIN_REDIRECTS", source)


class AuditSubmodules(WrapperCase):
    """R24: a gitlink in the base tree is not descended into by the launcher's audit: a nested
    repository the worker planted at it must not get its own config (a clean filter) run by the
    launcher's git."""

    def setUp(self):
        super().setUp()
        head = sp.git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        sp.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
        sp.git(self.repo, "commit", "-q", "-m", "add a gitlink")
        self.worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(self.worktree))
        self.layout = lw.worktree_git_layout(self.worktree)
        self.base = sp.git(self.worktree, "rev-parse", "HEAD").stdout.decode().strip()
        self.sentinel = self.root / "sentinel"

    def plant_nested_repo(self):
        nested = self.worktree / "sub"
        nested.mkdir(exist_ok=True)
        sp.git(nested, "init", "-q")
        (nested / "x.txt").write_text("one\n")
        sp.git(nested, "add", "x.txt")
        sp.git(nested, "commit", "-q", "-m", "nested")
        sp.git(nested, "config", "filter.pwn.clean", f"sh -c 'echo tampered > {self.sentinel}; cat'")
        (nested / ".git" / "info").mkdir(exist_ok=True)
        (nested / ".git" / "info" / "attributes").write_text("* filter=pwn\n")
        (nested / "x.txt").write_text("two\n")  # modified, so any status of it hashes through the filter
        if self.sentinel.exists():
            self.sentinel.unlink()

    def test_a_nested_repo_at_a_base_gitlink_does_not_run_its_filter_in_the_audit(self):
        self.plant_nested_repo()
        (self.worktree / "add.py").write_text("def add(a, b):\n    return a + b\n")
        audit = lw.audit_worktree(self.worktree, self.base, ["add.py"], [], scratch_dir=self.root,
                                  git_layout=self.layout)
        self.assertFalse(self.sentinel.exists(), "the nested repository's filter ran in the audit")
        # The rest of the audit is unaffected: the worker's real change is still reported.
        self.assertEqual(audit.changed_paths, ["add.py"])
        self.assertEqual(audit.violations, [])
        self.assertIn(b"add.py", audit.patch)
        self.assertNotIn(b"sub", audit.patch.replace(b"subtract", b""))

    def test_base_gitlinks_are_found_from_the_base_tree_including_nested_ones(self):
        head = self.base
        deep = self.repo / "vendor" / "lib"
        deep.mkdir(parents=True)
        (self.repo / "vendor" / "keep.txt").write_text("k\n")
        sp.git(self.repo, "add", "vendor/keep.txt")
        sp.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
        sp.git(self.repo, "commit", "-q", "-m", "nested gitlink")
        base = sp.git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        self.assertEqual(lw.base_gitlinks(self.worktree, base, env=self.layout), ["sub", "vendor/lib"])

    def test_a_repo_without_gitlinks_has_none(self):
        plain = sp.make_repo(self.root / "plain-parent")
        base = sp.git(plain, "rev-parse", "HEAD").stdout.decode().strip()
        self.assertEqual(lw.base_gitlinks(plain, base), [])



@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
class RealAuditRegression(WrapperCase):
    """Item 1: a wrapped worker must not make the launcher's unwrapped git run its code, via the
    worktree gitfile (variant a) or the linked worktree's commondir (variant b)."""

    def setUp(self):
        super().setUp()
        self.worktree = self.state / "worktrees" / "job-1"
        self.report_dir = self.state / "reports" / "job-1"
        (self.report_dir / "codex-home").mkdir(parents=True)
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(self.worktree))
        self.layout = lw.worktree_git_layout(self.worktree)
        self.base = sp.git(self.worktree, "rev-parse", "HEAD").stdout.decode().strip()
        self.sentinel = self.root / "sentinel"

    def profile(self):
        text = permissions.seatbelt_profile(
            permissions.default_permissions(), primary_repo=self.repo, state_dir=self.state,
            allowed_writes=lw.job_writable_paths(self.report_dir, self.worktree,
                                                 self.report_dir / "codex-home", 1, {}),
            protect_write_roots=lw.job_write_roots(self.report_dir, self.worktree,
                                                   self.report_dir / "codex-home"),
            job_git_admin=self.layout["GIT_DIR"], job_worktree=self.worktree)
        path = self.report_dir / sp.DENY_PROFILE_NAME
        path.write_text(text, encoding="utf-8")
        probe = subprocess.run(permissions.wrap_argv(["/usr/bin/true"], path), capture_output=True)
        if probe.returncode == NESTED_SANDBOX:
            self.skipTest("already inside a sandbox")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return path

    def sandboxed(self, profile, argv):
        env = dict(os.environ, **sp.GIT_ID)
        return subprocess.run(permissions.wrap_argv(argv, profile), capture_output=True, env=env)

    def make_attacker(self, kind):
        """A standalone git dir with a clean filter + attributes that would write the sentinel."""
        evil = self.root / ("evil-" + kind)
        subprocess.run(["git", "init", "-q", str(evil)], capture_output=True,
                       env=dict(os.environ, **sp.GIT_ID), check=True)
        gitdir = evil / ".git"
        subprocess.run(["git", "-C", str(evil), "config", "filter.pwn.clean",
                        f"sh -c 'echo tampered > {self.sentinel}; cat'"], check=True,
                       capture_output=True)
        (gitdir / "info").mkdir(exist_ok=True)
        (gitdir / "info" / "attributes").write_text("* filter=pwn\n")
        (gitdir / "HEAD").write_text(f"{self.base}\n")
        (gitdir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (gitdir / "objects" / "info" / "alternates").write_text(str(self.repo / ".git" / "objects") + "\n")
        return gitdir

    def test_the_profile_denies_repointing_the_gitfile_or_the_commondir(self):
        profile = self.profile()
        admin = Path(self.layout["GIT_DIR"])
        attempts = {
            "gitfile": (self.worktree / ".git", "gitdir: /tmp/evil"),
            "commondir": (admin / "commondir", "/tmp/evil"),
            "gitdir": (admin / "gitdir", "/tmp/evil"),
        }
        for name, (target, content) in attempts.items():
            with self.subTest(name=name):
                outcome = self.sandboxed(profile, ["/bin/sh", "-c", 'echo "$2" > "$1"', "sh",
                                                   str(target), content])
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)

    def _run_audit_after_tamper(self, kind):
        """Simulate the pre-fix hole: repoint a redirect file directly (unwrapped), then run the
        launcher's audit with the pinned layout and confirm the filter never ran."""
        before = lw.git_admin_fingerprint(self.layout, self.worktree)
        gitdir = self.make_attacker(kind)
        (self.worktree / "add.py").write_text("def add(a, b):\n    return a + b\n")
        if kind == "gitfile":
            (self.worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        else:
            Path(self.layout["GIT_DIR"], "commondir").write_text(f"{gitdir}\n")
        report = {"_git_layout": self.layout, "_git_admin_before": before, "violations": []}
        if self.sentinel.exists():
            self.sentinel.unlink()
        audit = lw.audit_worktree(self.worktree, self.base, ["add.py"], [],
                                  scratch_dir=self.report_dir, git_layout=self.layout)
        changed = lw.note_git_admin_tamper(report, self.worktree)
        return report, audit, changed

    def test_a_repointed_gitfile_does_not_steer_the_audit_and_is_flagged(self):
        report, audit, changed = self._run_audit_after_tamper("gitfile")
        self.assertFalse(self.sentinel.exists(), "the attacker filter ran in the launcher's git")
        self.assertIn("gitfile", changed)
        self.assertTrue(any(v["path"] == ".git" for v in report["violations"]))

    def test_a_repointed_commondir_does_not_steer_the_audit_and_is_flagged(self):
        report, audit, changed = self._run_audit_after_tamper("commondir")
        self.assertFalse(self.sentinel.exists(), "the attacker filter ran in the launcher's git")
        self.assertIn("commondir", changed)
        self.assertTrue(any(v["path"] == ".git" for v in report["violations"]))


if __name__ == "__main__":
    unittest.main()
