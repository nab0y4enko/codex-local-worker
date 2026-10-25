"""The worker runs in its own Codex home, on a generated model catalog, in an ephemeral session.

Nothing here talks to a server, a model or the installed Codex: discovery is ``sp.FakeOmlx``,
the worker is ``sp.FakeRun`` and the compatibility check is ``sp.fake_help``. Every git call is a
real disposable repository.
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import codex_catalog  # noqa: E402

lw = sp.lw


def failing_tests_run(**kwargs):
    """A worker that edits nothing useful, so verification fails and a second attempt runs."""
    return sp.FakeRun(test_rc=1, **kwargs)


class IsolationCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.profile = sp.discovered_profile()

    def run_clean_job(self, **kwargs):
        return sp.run_clean_job(self.root, self.repo, **kwargs)

    def argv_with_catalog(self):
        """A worker argv on a catalog generated for ``self.profile`` at a 32768 window."""
        report_dir = self.root / "report"
        report_dir.mkdir()
        lw.job_codex_home(report_dir)
        self.catalog = report_dir / codex_catalog.CATALOG_NAME  # the launcher's, not the worker's, file
        self.catalog_sha = codex_catalog.write_catalog(
            self.catalog, codex_catalog.build_local_entry(self.profile.model, 32768))
        return lw.worker_argv("codex", Path("/wt"), self.profile, 32768, Path("/r/l.txt"),
                              catalog_path=self.catalog)


class JobHome(IsolationCase):
    def test_capture_checkout_git_environment_drops_private_object_redirects(self):
        source = {"GIT_OBJECT_DIRECTORY": "/job/private/objects",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/primary/objects",
                  "IBToolNeverDeque": "1", "PATH": "/usr/bin"}
        captured = lw.capture_source_env(source)
        self.assertNotIn("GIT_OBJECT_DIRECTORY", captured)
        self.assertNotIn("GIT_ALTERNATE_OBJECT_DIRECTORIES", captured)
        self.assertEqual(captured["PATH"], source["PATH"])
        self.assertEqual(captured["IBToolNeverDeque"], "1")

    def test_worker_never_sees_the_users_codex_home(self):
        user_home = sp.tmpdir()
        self.addCleanup(sp.cleanup, user_home)
        (user_home / "AGENTS.md").write_text("route to launcher\n")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(user_home)}):
            report, run = self.run_clean_job()
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        worker = run.calls_of("exec")[0]
        self.assertNotEqual(worker["env"]["CODEX_HOME"], str(user_home))
        self.assertTrue(worker["env"]["CODEX_HOME"].startswith(report["report_dir"]))
        self.assertIn("--ephemeral", worker["argv"])
        self.assertEqual(lw.config_overrides_map(worker["argv"])["model_catalog_json"],
                         json.dumps(str(Path(report["report_dir"]) / "local-model-catalog.json")))

    def test_the_job_home_is_created_empty_inside_the_report_directory(self):
        report_dir = self.root / "report"
        report_dir.mkdir()
        home = lw.job_codex_home(report_dir)
        self.assertEqual(home, report_dir / "codex-home")
        self.assertTrue(home.is_dir())
        self.assertEqual(list(home.iterdir()), [])
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_a_home_that_already_holds_files_is_refused(self):
        report_dir = self.root / "report"
        (report_dir / "codex-home").mkdir(parents=True)
        (report_dir / "codex-home" / "auth.json").write_text("{}")
        with self.assertRaises(lw.BlockedError):
            lw.job_codex_home(report_dir)
        other = self.root / "other-report"
        other.mkdir()
        os.symlink(str(self.root), str(other / "codex-home"))
        with self.assertRaises(lw.BlockedError):
            lw.job_codex_home(other)

    def test_a_home_that_cannot_be_inspected_or_locked_down_is_a_blocked_job(self):
        report_dir = self.root / "report"
        (report_dir / "codex-home").mkdir(parents=True)
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(Path, "iterdir", side_effect=denied):
            with self.assertRaises(lw.BlockedError):
                lw.job_codex_home(report_dir)
        fresh = self.root / "fresh"
        fresh.mkdir()
        with mock.patch.object(Path, "chmod", side_effect=denied):
            with self.assertRaises(lw.BlockedError):
                lw.job_codex_home(fresh)

    def test_the_report_names_the_home_and_the_hash_of_the_catalog_it_launched_with(self):
        report, run = self.run_clean_job()
        home = Path(report["report_dir"]) / "codex-home"
        catalog = Path(report["report_dir"]) / "local-model-catalog.json"  # not in the writable home
        self.assertEqual(report["codex_home"], str(home))
        self.assertEqual(report["environment"]["codex_home"], str(home))
        self.assertEqual(report["catalog_sha256"], hashlib.sha256(catalog.read_bytes()).hexdigest())
        entry = json.loads(catalog.read_text(encoding="utf-8"))["models"][0]
        self.assertEqual(entry["slug"], report["api_model"])
        self.assertEqual(entry["context_window"], report["context_window"])

    def test_every_attempt_runs_in_the_same_home_on_the_same_catalog(self):
        report, run = self.run_clean_job(fake_run=failing_tests_run(worker_edit=sp.fix_add),
                                         max_attempts=2)
        workers = run.calls_of("exec")
        self.assertEqual(len(workers), 2)
        homes = {worker["env"]["CODEX_HOME"] for worker in workers}
        catalogs = {lw.config_overrides_map(worker["argv"])["model_catalog_json"]
                    for worker in workers}
        self.assertEqual(homes, {report["codex_home"]})
        self.assertEqual(len(catalogs), 1)

    def test_verification_commands_never_get_the_users_codex_home_either(self):
        user_home = sp.tmpdir()
        self.addCleanup(sp.cleanup, user_home)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(user_home)}):
            report, run = self.run_clean_job()
        others = [call for call in run.calls if call["argv"][1:2] != ["exec"]]
        self.assertTrue(others)
        for call in others:
            self.assertNotEqual(call["env"].get("CODEX_HOME"), str(user_home))

    def test_the_tool_probe_uses_the_job_home(self):
        seen = {}

        def probe(loaded, **kwargs):
            seen.update(kwargs)
            return loaded["evidence"]

        with mock.patch.object(lw, "probe_tools", probe):
            report, _ = self.run_clean_job()
        self.assertEqual(seen["codex_home"], Path(report["codex_home"]))
        overrides = lw.config_overrides_map(seen["overrides"])
        self.assertEqual(overrides["model_catalog_json"],
                         json.dumps(str(Path(report["report_dir"]) / codex_catalog.CATALOG_NAME)))
        self.assertNotIn("report_dir", seen)

    def test_a_catalog_changed_after_it_was_written_blocks_before_any_worker(self):
        real = codex_catalog.write_catalog

        def write_then_drift(path, entry):
            digest = real(path, entry)
            Path(path).write_text("{}", encoding="utf-8")
            return digest

        with mock.patch.object(lw.codex_catalog, "write_catalog", write_then_drift):
            report, run = self.run_clean_job()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("model_catalog_json", report["failure_reason"])
        self.assertEqual(run.calls_of("exec"), [])


class SelfCheck(IsolationCase):
    def test_read_argv_pins_native_profile_strict_config_schema_and_omits_sandbox_mode(self):
        self.argv_with_catalog()
        state = self.root / "state"
        worktree = state / "worktrees" / "read-job"
        worktree.mkdir(parents=True)
        permission_config = lw.native_read_profile_overrides(
            lw.permissions.default_permissions(), state, worktree)
        schema = self.root / "answer.schema.json"
        schema.write_text("{}", encoding="utf-8")
        argv = lw.worker_argv("codex", worktree, self.profile, 32768,
                              self.root / "last-message.txt", catalog_path=self.catalog,
                              sandbox_mode=None, output_schema=schema,
                              permission_config=permission_config, strict_config=True)
        self.assertEqual(lw.missing_config_pins(argv, self.profile, 32768,
            (self.catalog, self.catalog_sha), sandbox_mode=None,
            output_schema=schema, permission_config=permission_config, strict_config=True), [])
        self.assertNotIn("-s", argv)
        self.assertNotIn("sandbox_mode", lw.config_overrides_map(argv))
        self.assertIn("--output-schema", argv)
        self.assertIn("--strict-config", argv)
        for drift in ([item for item in argv if item != "--strict-config"],
                      [*argv, "-s", "read-only"]):
            self.assertTrue(lw.missing_config_pins(drift, self.profile, 32768,
                (self.catalog, self.catalog_sha), sandbox_mode=None,
                output_schema=schema, permission_config=permission_config, strict_config=True))

    def test_read_argv_without_a_schema_refuses_a_stray_output_schema_flag(self):
        self.argv_with_catalog()
        state = self.root / "state"
        worktree = state / "worktrees" / "read-job"
        worktree.mkdir(parents=True)
        permission_config = lw.native_read_profile_overrides(
            lw.permissions.default_permissions(), state, worktree)
        argv = lw.worker_argv("codex", worktree, self.profile, 32768,
                              self.root / "last-message.txt", catalog_path=self.catalog,
                              sandbox_mode=None, permission_config=permission_config,
                              strict_config=True)
        pins = dict(catalog=(self.catalog, self.catalog_sha), sandbox_mode=None,
                    permission_config=permission_config, strict_config=True)
        self.assertEqual(lw.missing_config_pins(argv, self.profile, 32768, **pins), [])
        drifted = [*argv[:-2], "--output-schema", str(self.root / "s.json"), *argv[-2:]]
        problems = lw.missing_config_pins(drifted, self.profile, 32768, **pins)
        self.assertTrue(any("--output-schema" in item for item in problems), problems)

    def test_catalog_drift_is_refused(self):
        argv = self.argv_with_catalog()
        Path(self.catalog).write_text("{}")
        self.assertTrue(lw.missing_config_pins(argv, self.profile, 32768,
                                               (self.catalog, self.catalog_sha)))

    def test_the_generated_catalog_passes_the_self_check(self):
        argv = self.argv_with_catalog()
        self.assertEqual(lw.missing_config_pins(argv, self.profile, 32768,
                                                (self.catalog, self.catalog_sha)), [])
        lw._policy_check(argv, self.profile, 32768, (self.catalog, self.catalog_sha))

    def test_a_generated_catalog_must_be_pinned_by_its_path(self):
        argv = self.argv_with_catalog()
        unpinned = lw.worker_argv("codex", Path("/wt"), self.profile, 32768, Path("/r/l.txt"))
        other = [item.replace(str(self.catalog), "/elsewhere/catalog.json") for item in argv]
        for label, drifted in (("no catalog pin", unpinned), ("another path", other)):
            with self.subTest(label=label):
                problems = " ".join(lw.missing_config_pins(drifted, self.profile, 32768,
                                                           (self.catalog, self.catalog_sha)))
                self.assertIn("model_catalog_json", problems)

    def test_a_catalog_the_launcher_did_not_generate_is_refused(self):
        argv = self.argv_with_catalog()
        problems = " ".join(lw.missing_config_pins(argv, self.profile, 32768))
        self.assertIn("model_catalog_json", problems)

    def test_a_deleted_or_rewritten_catalog_is_refused(self):
        argv = self.argv_with_catalog()
        wrong_hash = (self.catalog, "0" * 64)
        self.assertTrue(lw.missing_config_pins(argv, self.profile, 32768, wrong_hash))
        self.catalog.unlink()
        problems = " ".join(lw.missing_config_pins(argv, self.profile, 32768,
                                                   (self.catalog, self.catalog_sha)))
        self.assertIn("model_catalog_json", problems)

    def test_every_session_is_ephemeral(self):
        argv = self.argv_with_catalog()
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ephemeral", lw.REQUIRED_WORKER_FLAGS)
        drifted = [item for item in argv if item != "--ephemeral"]
        problems = lw.missing_config_pins(drifted, self.profile, 32768,
                                          (self.catalog, self.catalog_sha))
        self.assertIn("--ephemeral is missing", problems)

    def test_host_skills_and_goal_tools_are_switched_off_and_pinned(self):
        argv = self.argv_with_catalog()
        overrides = lw.config_overrides_map(argv)
        expected = {"skills.include_instructions": "false", "skills.bundled.enabled": "false",
                    "features.goals": "false"}
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(overrides[key], value)
                drifted = [item for item in argv if not item.startswith(key + "=")]
                self.assertTrue(lw.missing_config_pins(drifted, self.profile, 32768,
                                                       (self.catalog, self.catalog_sha)))
        # The under-development flag isolated nothing and only added an error item to every run.
        self.assertNotIn("features.skip_host_skill_discovery", overrides)
        self.assertNotIn("features.skip_host_skill_discovery", lw.FEATURE_FLAGS)


class Compatibility(IsolationCase):
    def test_codex_compatibility_is_checked_at_preflight(self):
        compat = lw.codex_compat("codex", run=sp.fake_help(version="codex-cli 0.163.0"))
        self.assertFalse(compat["ok"]); self.assertIn("0.164.0", compat["reason"])
        self.assertTrue(lw.codex_compat("codex", run=sp.fake_help(version="codex-cli 0.164.0"))["ok"])

    def test_compatibility_stops_after_version_probe_uses_the_job_deadline(self):
        class Budget:
            def __init__(self):
                self.values = iter((2.0, 0.0))

            def remaining(self):
                return next(self.values)

        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"codex-cli 0.164.0", b"")

        result = lw.codex_compat("codex", run=run, deadline=Budget())
        self.assertFalse(result["ok"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(calls, [["codex", "--version"]])

    def test_changed_model_diagnostic_does_not_probe_again_after_deadline(self):
        class Budget:
            def __init__(self):
                self.values = iter((2.0, 0.0))

            def remaining(self):
                return next(self.values)

        settings = sp.write_settings(self.root / "omlx" / "settings.json")
        fake = sp.FakeOmlx("none_loaded")
        result = lw.recheck_pinned_model(self.profile, settings, fake, 1, Budget())
        self.assertEqual(result[0], "model_changed")
        self.assertIn("budget_exhausted", result[1])
        self.assertEqual(fake.calls, ["/v1/models/status"])

    def test_the_minimum_version_and_the_required_flags(self):
        self.assertEqual(lw.MIN_CODEX_VERSION, (0, 164, 0))
        self.assertEqual(sorted(lw.CODEX_REQUIRED_FLAGS),
                         sorted(["--ephemeral", "--output-schema", "--ignore-user-config",
                                 "--ignore-rules", "--json", "--strict-config"]))

    def test_a_compatible_codex_reports_its_version_and_both_calls(self):
        run = sp.fake_help()
        compat = lw.codex_compat("/opt/codex", run=run)
        self.assertEqual(compat, {"ok": True, "version": "0.164.0", "missing_flags": [],
                                  "reason": None})
        self.assertEqual(run.calls, [["/opt/codex", "--version"],
                                     ["/opt/codex", "exec", "--help"]])

    def test_newer_and_prerelease_versions_pass(self):
        for version in ("codex-cli 0.164.1", "codex-cli 0.170.0", "codex-cli 1.0.0",
                        "codex-cli 0.165.0-alpha.3"):
            with self.subTest(version=version):
                self.assertTrue(lw.codex_compat("codex", run=sp.fake_help(version))["ok"])

    def test_a_prerelease_of_the_minimum_version_is_below_the_floor(self):
        for version in ("codex-cli 0.164.0-alpha.1", "codex-cli 0.164.0-rc.2"):
            with self.subTest(version=version):
                compat = lw.codex_compat("codex", run=sp.fake_help(version))
                self.assertFalse(compat["ok"])
                self.assertIn("0.164.0", compat["reason"])

    def test_the_probes_run_with_a_minimal_env_and_a_throwaway_codex_home(self):
        seen = []

        def bounded(argv, *, cwd, env, timeout):
            home = Path(env["CODEX_HOME"])
            seen.append((list(argv), dict(env), home.is_dir()))
            return {"returncode": 0, "stdout": b"codex-cli 0.164.0 --ephemeral", "stderr": b""}

        user_home = str(self.root / "user-codex")
        with mock.patch.dict(os.environ, {"CODEX_HOME": user_home, "OPENAI_API_KEY": "x",
                                          "HTTPS_PROXY": "http://proxy.invalid"}), \
                mock.patch.object(lw, "run_bounded", bounded):
            lw._run_codex_info("codex", ["--version"], subprocess.run)
        (argv, env, existed), = seen
        self.assertTrue(existed)
        self.assertNotEqual(env["CODEX_HOME"], user_home)
        self.assertFalse(Path(env["CODEX_HOME"]).exists(), "the throwaway home is removed")
        self.assertLessEqual(set(env), set(lw.BASE_ENV_KEYS) | {"CODEX_HOME"})

    def test_every_required_flag_must_be_listed(self):
        for flag in lw.CODEX_REQUIRED_FLAGS:
            with self.subTest(flag=flag):
                compat = lw.codex_compat("codex", run=sp.fake_help(missing=[flag]))
                self.assertFalse(compat["ok"])
                self.assertEqual(compat["missing_flags"], [flag])
                self.assertIn(flag, compat["reason"])

    def test_native_read_profile_is_read_only_and_hides_state_and_denied_paths(self):
        state = self.root / "state"
        worktree = state / "worktrees" / "job"
        state.mkdir()
        worktree.mkdir(parents=True)
        denied = self.root / "private" / "secret.txt"
        denied.parent.mkdir()
        config = dict(lw.native_read_profile_overrides(
            lw.permissions.default_permissions(), state, worktree, extra_deny_read=[denied]))
        self.assertEqual(config["default_permissions"], json.dumps(lw.READ_PERMISSION_PROFILE))
        self.assertEqual(config["permissions.%s.extends" % lw.READ_PERMISSION_PROFILE],
                         json.dumps(":read-only"))
        filesystem = config["permissions.%s.filesystem" % lw.READ_PERMISSION_PROFILE]
        for path, action in ((state.resolve(), "deny"), (denied.resolve(), "deny")):
            self.assertIn(json.dumps(str(path)) + ' = { "." = ' + json.dumps(action) + " }",
                          filesystem)
        self.assertNotIn(json.dumps(str(worktree.resolve())) + " = \"read\"", filesystem)
        self.assertIn('":workspace_roots" = { "." = "read" }', filesystem)
        self.assertEqual(config["permissions.%s.workspace_roots" % lw.READ_PERMISSION_PROFILE],
                         "{ " + json.dumps(str(worktree.resolve())) + " = true }")

    def test_bracketed_denials_use_nested_exact_path_rules(self):
        state = self.root / "state [literal]"
        worktree = state / "worktrees" / "job"
        worktree.mkdir(parents=True)
        denied = self.root / "private" / "secret [literal] file.txt"
        denied.parent.mkdir()
        config = dict(lw.native_read_profile_overrides(
            lw.permissions.default_permissions(), state, worktree, extra_deny_read=[denied]))
        filesystem = config["permissions.%s.filesystem" % lw.READ_PERMISSION_PROFILE]
        for path in (state.resolve(), denied.resolve()):
            self.assertIn(json.dumps(str(path)) + ' = { "." = "deny" }', filesystem)
        self.assertNotIn(json.dumps(str(state.resolve())) + ' = "deny"', filesystem)
        self.assertNotIn(json.dumps(str(denied.resolve())) + ' = "deny"', filesystem)

    def test_an_unreadable_version_or_a_missing_binary_is_not_compatible(self):
        compat = lw.codex_compat("codex", run=sp.fake_help(version="codex-cli dev"))
        self.assertFalse(compat["ok"])
        self.assertIsNone(compat["version"])

        def absent(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        compat = lw.codex_compat("/nowhere/codex", run=absent)
        self.assertFalse(compat["ok"])
        self.assertIn("/nowhere/codex", compat["reason"])

    def test_run_refuses_an_incompatible_codex_before_discovery_or_any_worker(self):
        omlx = sp.FakeOmlx("one")
        options = sp.job_options(self.root, self.repo, omlx=omlx)
        options.codex_run = sp.fake_help(version="codex-cli 0.163.0")
        fake = sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "preflight_failed")
        self.assertEqual(report["exit_code"], lw.EXIT_CODES["preflight_failed"])
        self.assertIn("0.164.0", report["failure_reason"])
        self.assertIs(report["codex_compat"]["ok"], False)
        self.assertEqual(omlx.calls, [])
        self.assertEqual(fake.calls_of("exec"), [])
        self.assertIsNone(report["worktree"])

    def test_preflight_refuses_an_incompatible_codex(self):
        settings = sp.write_settings(self.root / "omlx" / "settings.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lw.main(["preflight", "--settings", str(settings), "--codex-bin", "codex-mock"],
                           omlx_fetch=sp.FakeOmlx("one"),
                           codex_run=sp.fake_help(missing=["--ephemeral"]))
        payload = json.loads(out.getvalue())
        self.assertEqual(code, lw.EXIT_CODES["preflight_failed"])
        self.assertEqual(payload["status"], "preflight_failed")
        self.assertEqual(payload["codex_compat"]["missing_flags"], ["--ephemeral"])
        self.assertNotIn("discovery_error", payload)

    def test_preflight_reports_a_compatible_codex(self):
        settings = sp.write_settings(self.root / "omlx" / "settings.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lw.main(["preflight", "--settings", str(settings), "--codex-bin", "codex-mock"],
                           omlx_fetch=sp.FakeOmlx("one"), codex_run=sp.fake_help())
        receipt = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(receipt["codex_compat"]["version"], "0.164.0")
        self.assertEqual(receipt["profile"]["model"]["api_model"], sp.MODEL)


if __name__ == "__main__":
    unittest.main()
