"""Full-host execution routing: pinned worker argv, direct verification, native scope.

These tests pin the *default* routing introduced for full-host execution: the worker runs
``danger-full-access`` with approvals off, verification commands run directly (plain argv,
``sandboxed: false``) with their real output retained, and a native Apple opt-in records the
selected simulator without running any confinement probe. The legacy confined backends stay
reachable when a job explicitly selects them.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
APPLE = lw.apple
UDID = sp.APPLE_UDID
PY = sys.executable


def task(root, commands=None):
    return lw.Task(
        task_id="full-host-core", repo_path=Path(root), base_ref="main",
        goal="route execution to the full host", acceptance_criteria=["full host"],
        allowed_paths=["add.py"],
        test_commands=commands or [[PY, "-c", "print('FULLHOST-OK')"]],
        timeout_seconds=120, max_attempts=1)


def options(root, **overrides):
    kwargs = dict(task=task(Path(root)), state_dir=Path(root) / "state", codex_bin="codex",
                  verify=True, codex_run=sp.fake_help())
    kwargs.update(overrides)
    return lw.JobOptions(**kwargs)


def sandbox_result(returncode=0, stdout=b"", timed_out=False):
    return {"argv": [], "returncode": returncode, "timed_out": timed_out,
            "elapsed_seconds": 0.01, "stdout": stdout, "stderr": b"", "termination": {}}


class WorkerArgv(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.profile = sp.discovered_profile()
        self.window = self.profile.model.context_window
        self.argv = lw.worker_argv("codex", self.root, self.profile, self.window,
                                   self.root / "last-message.txt")
        self.overrides = lw.config_overrides_map(self.argv)

    def test_worker_runs_danger_full_access_and_never_exits_the_host_config(self):
        self.assertEqual(self.argv[self.argv.index("-s") + 1], "danger-full-access")
        self.assertIn("--ignore-rules", self.argv)
        self.assertIn("--ignore-user-config", self.argv)
        self.assertIn("--ephemeral", self.argv)

    def test_pinned_policy_is_full_host_and_self_checks_clean(self):
        self.assertEqual(self.overrides["sandbox_mode"], '"danger-full-access"')
        self.assertEqual(self.overrides["approval_policy"], '"never"')
        self.assertEqual(self.overrides["web_search"], '"disabled"')
        self.assertEqual(lw.CONFIG_PINS["sandbox_mode"], '"danger-full-access"')
        self.assertEqual(lw.missing_config_pins(self.argv, self.profile, self.window), [])
        lw._policy_check(self.argv, self.profile, self.window)

    def test_local_worker_routing_and_no_fallback_pins_are_unchanged(self):
        provider = f"model_providers.{lw.PROVIDER_ID}"
        alias = self.profile.model.api_model
        self.assertEqual(self.argv[self.argv.index("-m") + 1], alias)
        self.assertEqual(self.overrides["model"], f'"{alias}"')
        self.assertEqual(self.overrides["model_provider"], f'"{lw.PROVIDER_ID}"')
        self.assertEqual(self.overrides[f"{provider}.base_url"],
                         f'"{lw.profile_endpoint(self.profile)}"')
        self.assertEqual(self.overrides[f"{provider}.env_key"], f'"{lw.API_KEY_ENV}"')
        self.assertEqual(self.overrides[f"{provider}.request_max_retries"], "0")
        self.assertEqual(self.overrides[f"{provider}.stream_max_retries"], "0")
        self.assertIn(lw.API_KEY_ENV, self.overrides["shell_environment_policy.exclude"])
        for key, value in lw.FEATURE_FLAGS.items():
            self.assertEqual(self.overrides[key], "true" if value else "false")

    def test_sandboxed_worker_argv_is_now_a_policy_violation(self):
        drifted = [item.replace("danger-full-access", "workspace-write") for item in self.argv]
        with self.assertRaises(lw.BlockedError):
            lw._policy_check(drifted, self.profile, self.window)

    def test_git_and_audit_policies_are_preserved(self):
        self.assertIn("core.hooksPath=/dev/null", lw.GIT_GLOBAL_ARGS)
        self.assertIn("--no-ext-diff", lw.DIFF_ARGS)
        self.assertTrue(any("full-host" in item for item in lw.LIMITATIONS))
        self.assertFalse(any("workspace-write" in item for item in lw.LIMITATIONS))


class VerificationArgv(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.command = [PY, "-m", "unittest", "-v"]

    def test_full_host_returns_the_plain_command(self):
        argv = lw.verification_argv({"backend": lw.FULL_HOST_BACKEND, "available": True},
                                    "codex", self.root, self.command)
        self.assertEqual(argv, self.command)
        self.assertNotIn("codex", argv)
        self.assertNotIn("sandbox", argv)
        self.assertNotIn(lw.apple.SANDBOX_EXEC, argv)

    def test_legacy_codex_workspace_write_backend_still_wraps(self):
        argv = lw.verification_argv({"backend": lw.WORKSPACE_SANDBOX_BACKEND, "available": True},
                                    "codex", self.root, self.command)
        self.assertEqual(argv[:2], ["codex", "sandbox"])
        self.assertEqual(argv[argv.index("-P") + 1], lw.SANDBOX_PROFILE)
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1:], self.command)

    def test_legacy_apple_backend_still_requires_its_profile(self):
        with self.assertRaises(lw.BlockedError):
            lw.verification_argv({"backend": APPLE.BACKEND}, "codex", self.root, self.command)


class Probe(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.env = {"HOME": "/home/tester", "CODEX_HOME": "/codex/tester", "PATH": "/usr/bin"}

    def test_probe_runs_one_trivial_command_directly_with_no_wrapper(self):
        with mock.patch.object(lw, "run_bounded",
                               return_value=sandbox_result(stdout=b"__LOCAL_WORKER_SANDBOX_OK__\n")) as run:
            info = lw.probe_sandbox("codex", self.root, self.env, (), lw.Deadline(120))
        argv = run.call_args.args[0]
        self.assertEqual(list(argv), lw.FULL_HOST_PROBE_ARGV)
        self.assertNotIn("codex", argv)
        self.assertNotIn("sandbox", argv)
        self.assertEqual(info["backend"], lw.FULL_HOST_BACKEND)
        self.assertTrue(info["available"])

    def test_probe_metadata_reports_network_enabled_and_no_confinement(self):
        info = lw.probe_sandbox("codex", self.root, self.env, (), lw.Deadline(120))
        self.assertEqual(info["backend"], lw.FULL_HOST_BACKEND)
        self.assertEqual(info["network"], "enabled")
        self.assertEqual(info["profile"], lw.FULL_HOST_PROFILE)
        self.assertIn("full host", info["confinement"])
        self.assertTrue(info["available"], info["detail"])
        self.assertEqual(info["probe_returncode"], 0)

    def test_probe_keeps_the_legacy_confined_backend_available(self):
        with mock.patch.object(lw, "run_bounded",
                               return_value=sandbox_result(stdout=b"__LOCAL_WORKER_SANDBOX_OK__\n")) as run:
            info = lw.probe_sandbox("codex", self.root, self.env, (), lw.Deadline(120),
                                    backend=lw.WORKSPACE_SANDBOX_BACKEND)
        self.assertEqual(run.call_args.args[0][:2], ["codex", "sandbox"])
        self.assertEqual(info["backend"], lw.WORKSPACE_SANDBOX_BACKEND)
        self.assertEqual(info["network"], "disabled")

    def test_probe_reports_unavailable_without_claiming_confinement(self):
        with mock.patch.object(lw, "run_bounded", return_value=sandbox_result(returncode=127)):
            info = lw.probe_sandbox("codex", self.root, self.env, (), lw.Deadline(120))
        self.assertFalse(info["available"])
        self.assertEqual(info["network"], "enabled")
        self.assertEqual(info["profile"], lw.FULL_HOST_PROFILE)


class DirectVerification(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.sandbox = dict(lw.FULL_HOST_BACKEND and {"backend": lw.FULL_HOST_BACKEND},
                            available=True, network="enabled", profile=lw.FULL_HOST_PROFILE)
        self.env = {"HOME": "/home/tester", "CODEX_HOME": "/codex/tester", "PATH": "/usr/bin"}

    def run_commands(self, commands, sandbox=None):
        current = task(self.root, commands)
        return lw.run_verification(current, self.root, dict(self.env), "codex",
                                   sandbox or self.sandbox, [], lw.Deadline(120))

    def test_commands_run_directly_and_report_sandboxed_false(self):
        results = self.run_commands([[PY, "-c", "print('FULLHOST-OK')"]])
        self.assertEqual(len(results), 1)
        entry = results[0]
        self.assertEqual(entry["command"], [PY, "-c", "print('FULLHOST-OK')"])
        self.assertEqual(entry["status"], "passed")
        self.assertIs(entry["sandboxed"], False)
        self.assertEqual(entry["backend"], lw.FULL_HOST_BACKEND)
        self.assertIn("full host", entry["confinement"])
        self.assertEqual(entry["returncode"], 0)
        self.assertIn("FULLHOST-OK", entry["output_text"])

    def test_failure_keeps_output_and_says_the_host_ran_it(self):
        results = self.run_commands([[PY, "-c", "print('NOISE'); raise SystemExit(3)"]])
        entry = results[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIs(entry["sandboxed"], False)
        self.assertEqual(entry["returncode"], 3)
        self.assertIn("NOISE", entry["output_text"])
        self.assertIn("full-host", entry["reason"])

    def test_blocked_result_when_the_readiness_probe_did_not_pass(self):
        results = self.run_commands([[PY, "-c", "print('never')"]],
                                    sandbox={"backend": lw.FULL_HOST_BACKEND, "available": False,
                                             "detail": "probe refused"})
        entry = results[0]
        self.assertEqual(entry["status"], "blocked")
        self.assertIs(entry["sandboxed"], False)
        self.assertIn("probe refused", entry["reason"])
        self.assertNotIn("output_text", entry)

    def test_direct_command_env_keeps_home_and_pins_ibtool(self):
        with mock.patch.object(lw, "run_bounded", return_value=sandbox_result()) as run:
            self.run_commands([[PY, "-c", "pass"]])
        env = run.call_args.kwargs["env"]
        cwd = run.call_args.kwargs["cwd"]
        self.assertEqual(run.call_args.args[0], [PY, "-c", "pass"])
        self.assertEqual(cwd, self.root)
        self.assertEqual(env["IBToolNeverDeque"], "1")
        self.assertEqual(env["HOME"], "/home/tester")
        self.assertEqual(env["CODEX_HOME"], "/codex/tester")
        self.assertIsNone(env.get(APPLE.FOUNDATION_HOME_ENV))
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_executed_full_host_results_keep_their_log_files(self):
        """A real mocked-worker job keeps unsandboxed output and its log file."""
        repo = sp.make_repo(self.root)
        fake = sp.FakeRun(worker_edit=lambda cwd, n: (cwd / "add.py").write_text("fixed\n"))
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(sp.job_options(self.root, repo))
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["sandbox"]["backend"], lw.FULL_HOST_BACKEND)
        entry = report["attempts"][0]["tests"][0]
        self.assertEqual(entry["status"], "passed")
        self.assertIs(entry["sandboxed"], False)      # full host: nothing confined it
        self.assertNotIn("output_text", entry)        # drained into the log file, not the report
        log_path = Path(entry["log_path"])
        self.assertTrue(log_path.is_file())
        self.assertIn("Ran 1 test", log_path.read_text(encoding="utf-8"))
        self.assertIn("OK", log_path.read_text(encoding="utf-8"))
        self.assertIn(str(log_path), report["logs"])

    def test_ibtool_never_deque_is_forced_over_a_hostile_incoming_value(self):
        incoming = {"IBToolNeverDeque": "0", "HOME": "/home/tester", "PATH": "/usr/bin"}
        bounded = lw.full_host_env(incoming)
        self.assertEqual(bounded["IBToolNeverDeque"], "1")
        self.assertEqual(incoming["IBToolNeverDeque"], "0")      # caller dict untouched
        outcome = sp.result(list(lw.FULL_HOST_PROBE_ARGV), 0, lw.PROBE_MARKER)
        with mock.patch.object(lw, "run_bounded", return_value=outcome) as run:
            probe = lw.probe_sandbox("codex", self.root, dict(incoming))
            results = lw.run_verification(task(self.root, [[PY, "-c", "print('x')"]]),
                                          self.root, dict(incoming), "codex", probe, [],
                                          lw.Deadline(120))
        self.assertEqual(results[0]["status"], "passed")
        self.assertEqual(run.call_args.kwargs["env"]["IBToolNeverDeque"], "1")


class NativeScope(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.opts = options(self.root, apple_simulator_udid=UDID,
                            native_preflight_plan=self.root / "plan.json")
        self.report = lw.new_report(self.opts)
        self.report["report_dir"] = str(self.report_dir)
        self.probed = lw.probe_sandbox("codex", self.root, {}, (), lw.Deadline(120))

    def opt_in(self):
        with mock.patch.object(APPLE, "probe_apple") as probe, \
                mock.patch.object(APPLE, "sandbox_argv") as wrap:
            sandbox = lw._apple_opt_in(self.opts, self.report, self.report_dir, self.root,
                                       dict(self.probed), {}, [], lw.Deadline(120))
        self.assertFalse(probe.called, "native setup must not run a confinement probe")
        self.assertFalse(wrap.called, "native setup must not build a sandbox profile")
        return sandbox

    def test_native_opt_in_selects_full_host_without_a_profile(self):
        sandbox = self.opt_in()
        self.assertEqual(sandbox["backend"], lw.FULL_HOST_BACKEND)
        self.assertEqual(sandbox["profile"], lw.FULL_HOST_PROFILE)
        self.assertEqual(sandbox["network"], "enabled")
        self.assertEqual(sandbox["apple_simulator_udid"], UDID)
        self.assertEqual(sandbox["native_env"], {"IBToolNeverDeque": "1"})
        self.assertEqual(self.report["sandbox"]["backend"], lw.FULL_HOST_BACKEND)
        self.assertEqual(self.report["sandbox"]["apple"]["simulator_udid"], UDID)
        self.assertEqual(self.report["sandbox"]["apple"]["sandbox_exec"], None)
        self.assertEqual(self.report["sandbox"]["apple"]["env_preserved"],
                         ["HOME", "CODEX_HOME"])
        self.assertEqual(list(self.report_dir.glob("*.sb")), [])

    def test_native_verification_and_worker_commands_stay_plain(self):
        sandbox = self.opt_in()
        command = ["xcrun", "simctl", "list", "devices"]
        argv = lw.verification_argv(sandbox, "codex", self.root, command)
        self.assertEqual(argv, command)
        env = lw.full_host_env({"HOME": "/home/tester", "CODEX_HOME": "/codex/tester",
                                "TMPDIR": "/tmp/job"})
        self.assertEqual(env["IBToolNeverDeque"], "1")
        self.assertEqual(env["HOME"], "/home/tester")
        self.assertEqual(env["CODEX_HOME"], "/codex/tester")
        self.assertEqual(env["TMPDIR"], "/tmp/job")
        self.assertIsNone(env.get(APPLE.FOUNDATION_HOME_ENV))
        self.assertIsNone(env.get("CLANG_MODULE_CACHE_PATH"))

    def test_native_gate_uses_the_plain_verification_environment(self):
        sandbox = self.opt_in()
        verify_env = {"HOME": "/home/tester", "CODEX_HOME": "/codex/tester"}
        captured = {}

        def qualify(plan, worktree, report_dir, udid, confined, deadline):
            confined(["xcrun", "simctl", "list", "devices"])
            return {"udid": udid}

        with mock.patch.object(lw.native, "qualify", side_effect=qualify), \
                mock.patch.object(lw, "run_bounded", return_value=sandbox_result()) as run:
            lw.native_gate(self.opts, self.report, self.root, sandbox, verify_env, [],
                           lw.Deadline(120))
        self.assertEqual(run.call_args.args[0], ["xcrun", "simctl", "list", "devices"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["IBToolNeverDeque"], "1")
        self.assertEqual(env["HOME"], "/home/tester")
        self.assertIsNone(env.get(APPLE.FOUNDATION_HOME_ENV))
        self.assertEqual(self.report["native_preflight"], {"udid": UDID})

    def test_worker_environment_gets_the_native_toolchain_pin(self):
        self.assertEqual(lw.full_host_env({}).get("IBToolNeverDeque"), "1")
        note = lw.APPLE_PROMPT_NOTE.format(udid=UDID)
        self.assertIn(UDID, note)
        self.assertIn("full host", note)
        self.assertIn("simctl", note)
        self.assertNotIn("Seatbelt", note)
        self.assertNotIn("workspace-write", note)


class Report(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)

    def test_report_declares_full_host_execution(self):
        report = lw.new_report(options(self.root))
        self.assertEqual(report["sandbox_mode"], "danger-full-access")
        self.assertEqual(report["sandbox_profile"], lw.FULL_HOST_PROFILE)
        self.assertTrue(any("full-host" in item for item in report["limitations"]))

    def test_status_wording_no_longer_promises_a_sandbox(self):
        joined = " ".join(lw.STATUS_REASONS.values())
        self.assertNotIn("sandbox", joined.lower())
        report = lw.new_report(options(self.root))
        report["status"] = "verification_blocked"
        report["tests"] = []
        self.assertIn("no verification evidence", lw.describe_failure(report))
        self.assertNotIn("sandbox", lw.describe_failure(report).lower())

    def test_summary_lines_report_the_full_host_backend(self):
        report = lw.new_report(options(self.root))
        report["sandbox"] = {"backend": lw.FULL_HOST_BACKEND, "available": True,
                             "apple_simulator_udid": UDID}
        report["exit_code"] = 0
        lines = "\n".join(lw.summary_lines(report))
        self.assertIn(lw.FULL_HOST_BACKEND, lines)
        self.assertIn("confined=no", lines)
        self.assertIn(UDID, lines)


if __name__ == "__main__":
    unittest.main()
