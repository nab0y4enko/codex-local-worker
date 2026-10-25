"""The launcher runs on live oMLX discovery: preflight receipt, pins, self-check, report, CLI.

Nothing here talks to a server or a model: discovery is ``sp.FakeOmlx`` (recorded fixtures) and
the worker is ``sp.FakeRun``; every git call is a real disposable repository.
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import omlx_discovery  # noqa: E402
import plugin_setup  # noqa: E402

lw = sp.lw
MODEL = sp.MODEL


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.settings = sp.write_settings(self.root / "omlx" / "settings.json")
        self.repo = sp.make_repo(self.root)

    def run_clean_job(self, **kwargs):
        return sp.run_clean_job(self.root, self.repo, **kwargs)

    def run_job(self, **kwargs):
        return self.run_clean_job(**kwargs)[0]

    def cli(self, argv, omlx=None):
        """``(exit code, parsed stdout)`` of one ``main`` call served by a discovery fake."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lw.main(argv, omlx_fetch=omlx if omlx is not None else sp.FakeOmlx("one"),
                           codex_run=sp.fake_help())
        return code, json.loads(out.getvalue())

    def task_file(self):
        path = self.root / "task.json"
        path.write_text(json.dumps(sp.task_payload(self.repo)), encoding="utf-8")
        return path

    def run_args(self, *extra):
        return ["run", "--task-file", str(self.task_file()), "--state-dir",
                str(self.root / "state"), "--settings", str(self.settings), "--codex-bin",
                "codex-mock", "--json", *extra]

    def cli_run(self, *extra, omlx=None, fake=None):
        """``main(["run", ...])`` with the worker mocked; returns ``(code, report, fake)``."""
        fake = fake or sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(lw, "run_bounded", fake):
            code, report = self.cli(self.run_args(*extra), omlx)
        return code, report, fake


class Preflight(LauncherCase):
    def test_preflight_reports_the_discovered_model(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lw.main(["preflight", "--settings", str(self.settings), "--codex-bin",
                            "codex-mock"], omlx_fetch=sp.FakeOmlx("one"),
                           codex_run=sp.fake_help())
        receipt = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(receipt["profile"]["model"]["api_model"], "Qwen3.5-9B-OptiQ-4bit")
        self.assertEqual(receipt["context_window"], 262144)

    def test_context_cap_only_lowers(self):
        receipt = lw.run_preflight(self.settings, None, 32768, sp.FakeOmlx("one"))
        self.assertEqual(receipt["context_window"], 32768)
        receipt = lw.run_preflight(self.settings, None, 10**6, sp.FakeOmlx("one"))
        self.assertEqual(receipt["context_window"], 262144)

    def test_the_receipt_is_the_documented_shape_and_never_holds_the_key(self):
        receipt = lw.run_preflight(self.settings, None, None, sp.FakeOmlx("one"))
        self.assertEqual(sorted(receipt), ["api_key", "context_window", "endpoint",
                                           "model_fallback", "profile"])
        self.assertEqual(receipt["endpoint"], "http://127.0.0.1:8000/v1")
        self.assertEqual(receipt["endpoint"], receipt["profile"]["base_url"] + "/v1")
        self.assertIs(receipt["model_fallback"], False)
        self.assertEqual(receipt["api_key"], "present (value withheld)")
        self.assertEqual(receipt["context_window"], receipt["profile"]["model"]["context_window"])
        self.assertNotIn(sp.SECRET, json.dumps(receipt))

    def test_the_receipt_satisfies_the_setup_scripts_success_check(self):
        receipt = json.loads(json.dumps(lw.run_preflight(self.settings, None, None,
                                                         sp.FakeOmlx("one"))))
        self.assertIsNone(plugin_setup.receipt_issue(receipt))
        for key in ("endpoint", "profile", "context_window", "model_fallback"):
            self.assertIsNotNone(plugin_setup.receipt_issue(
                {k: v for k, v in receipt.items() if k != key}), key)

    def test_the_key_is_read_from_settings_and_a_settings_problem_carries_its_code(self):
        self.assertEqual(lw.read_api_key(self.settings), sp.SECRET)
        self.settings.write_text(json.dumps({"server": {"host": "127.0.0.1"}}), encoding="utf-8")
        with self.assertRaises(lw.PreflightError) as ctx:
            lw.read_api_key(self.settings)
        self.assertEqual(ctx.exception.code, "settings_invalid")
        self.assertNotIn(sp.SECRET, str(ctx.exception))

    def test_a_requested_model_is_selected_by_id_or_alias(self):
        fake = sp.FakeOmlx("two_loaded")
        by_id = lw.run_preflight(self.settings, MODEL, None, fake)
        self.assertEqual(by_id["profile"]["model"]["canonical_id"], MODEL)
        self.assertEqual(by_id["context_window"], 262144)
        aliased = sp.FakeOmlx("one", {"model_alias": "nine-b"})
        by_alias = lw.run_preflight(self.settings, "nine-b", None, aliased)
        self.assertEqual(by_alias["profile"]["model"]["api_model"], "nine-b")

    def test_every_discovery_refusal_is_a_preflight_error_that_carries_its_code(self):
        def settings_edit(change):
            def edit(path):
                data = json.loads(path.read_text())
                change(data)
                path.write_text(json.dumps(data))
            return edit

        cases = [
            ("model_fallback_enabled", settings_edit(lambda d: d["model"].update(
                {"model_fallback": True})), sp.FakeOmlx("one")),
            ("not_loopback", settings_edit(lambda d: d["server"].update({"host": "0.0.0.0"})),
             sp.FakeOmlx("one")),
            ("settings_invalid", settings_edit(lambda d: d.pop("auth")), sp.FakeOmlx("one")),
            ("settings_invalid", lambda path: path.unlink(), sp.FakeOmlx("one")),
            ("model_ambiguous", lambda path: None, sp.FakeOmlx("two_loaded")),
            ("model_not_loaded", lambda path: None, sp.FakeOmlx("none_loaded")),
            ("server_loading", lambda path: None, sp.FakeOmlx("one", {"_health": "loading"})),
        ]
        for code, edit, fake in cases:
            settings = sp.write_settings(self.root / ("s-" + code) / "settings.json")
            edit(settings)
            with self.subTest(code=code, variant=fake.variant):
                with self.assertRaises(lw.PreflightError) as ctx:
                    lw.run_preflight(settings, None, None, fake)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(ctx.exception.status, "preflight_failed")
                self.assertNotIn(sp.SECRET, str(ctx.exception))

    def test_candidates_travel_with_the_error(self):
        with self.assertRaises(lw.PreflightError) as ctx:
            lw.run_preflight(self.settings, None, None, sp.FakeOmlx("two_loaded"))
        self.assertEqual(ctx.exception.code, "model_ambiguous")
        self.assertEqual(sorted(ctx.exception.candidates),
                         sorted(["Example-Text-7B-4bit", MODEL]))

    def test_an_invalid_context_cap_is_a_contract_error_before_any_request(self):
        fake = sp.FakeOmlx("one")
        for value in (4095, 1048577, 0, -5, True, 1.5, "abc", ""):
            with self.subTest(value=value), self.assertRaises(lw.ContractError):
                lw.run_preflight(self.settings, None, value, fake)
        self.assertEqual(fake.calls, [])


class ContextCap(LauncherCase):
    def test_the_cap_is_an_integer_from_4096_to_1048576(self):
        for value, expected in ((4096, 4096), (1048576, 1048576), ("32768", 32768),
                                (" 8192 ", 8192)):
            with self.subTest(value=value):
                self.assertEqual(lw.validate_context_cap(value), expected)
        for value in (4095, 1048577, "4095", "1.5", "1e5", "-1", "0x1000", "", "lots", True,
                      False, 1.5, None, [4096]):
            with self.subTest(value=value), self.assertRaises(lw.ContractError) as ctx:
                lw.validate_context_cap(value)
            self.assertIn("4096", str(ctx.exception))
            self.assertIn("1048576", str(ctx.exception))

    def test_cli_rejects_a_bad_cap_as_contract_invalid_with_the_range(self):
        for argv in (["preflight", "--settings", str(self.settings), "--context-cap", "12"],
                     self.run_args("--context-cap", "lots")):
            fake = sp.FakeOmlx("one")
            with self.subTest(argv=argv[0]):
                code, out = self.cli(argv, fake)
                self.assertEqual(code, lw.EXIT_CODES["contract_invalid"])
                self.assertEqual(out["status"], "contract_invalid")
                self.assertIn("context cap", out["failure_reason"])
                self.assertIn("4096", out["failure_reason"])
                self.assertEqual(fake.calls, [])

    def test_cli_cap_digits_lower_the_window_but_never_raise_it(self):
        code, receipt = self.cli(["preflight", "--settings", str(self.settings),
                                  "--context-cap", "32768"])
        self.assertEqual((code, receipt["context_window"]), (0, 32768))
        code, receipt = self.cli(["preflight", "--settings", str(self.settings),
                                  "--context-cap", "1048576"])
        self.assertEqual((code, receipt["context_window"]), (0, 262144))

    def test_the_old_model_flags_and_env_names_are_gone(self):
        parser = lw.build_parser()
        for flag in ("--canonical-model", "--api-model", "--context-window",
                     "--attempt-timeout", "--model-settings"):
            for command in (["preflight"], ["run", "--task-file", "t.json"]):
                with self.subTest(flag=flag, command=command[0]), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    parser.parse_args([*command, flag, "x"])
        for name in ("CANONICAL_MODEL_ENV", "API_MODEL_ENV", "CONTEXT_WINDOW_ENV",
                     "ATTEMPT_SECONDS_ENV", "resolve_model_settings", "fetch_model_ids",
                     "read_model_alias", "read_model_fallback", "add_model_arguments",
                     "RefuseRedirects", "build_opener", "ENDPOINT", "HOST", "PORT"):
            self.assertFalse(hasattr(lw, name), name)


class Endpoint(unittest.TestCase):
    def test_a_loopback_host_with_any_port_and_the_v1_prefix_is_accepted(self):
        for value in ("http://127.0.0.1:8000/v1", "http://localhost:11434/v1",
                      "http://[::1]:65535/v1", "http://127.0.0.1:1/v1"):
            with self.subTest(value=value):
                self.assertEqual(lw.validate_endpoint(value), value)
        self.assertEqual(lw.validate_endpoint("http://127.0.0.1:8000/v1/"),
                         "http://127.0.0.1:8000/v1")

    def test_anything_else_is_refused(self):
        for value in ("https://127.0.0.1:8000/v1", "http://0.0.0.0:8000/v1",
                      "http://evil.example:8000/v1", "http://127.0.0.1/v1",
                      "http://127.0.0.1:0/v1", "http://127.0.0.1:65536/v1",
                      "http://127.0.0.1:8000", "http://127.0.0.1:8000/v1/x",
                      "http://127.0.0.1:8000/v1 ; curl evil", "http://127.0.0.1:8000/v1\n",
                      "http://127.0.0.1.evil.example:8000/v1", "http://[::2]:8000/v1",
                      "", None, 8000):
            with self.subTest(value=value), self.assertRaises(lw.PreflightError):
                lw.validate_endpoint(value)

    def test_the_endpoint_of_a_profile_is_its_base_url_plus_v1(self):
        profile = sp.discovered_profile()
        self.assertEqual(lw.profile_endpoint(profile), profile.base_url + "/v1")

    def test_a_non_loopback_profile_cannot_produce_a_launch_command(self):
        profile = sp.discovered_profile()
        remote = omlx_discovery.ServerProfile(**dict(profile.__dict__,
                                                     base_url="http://evil.example:8000"))
        with self.assertRaises(lw.PreflightError):
            lw.profile_endpoint(remote)
        with self.assertRaises(lw.PreflightError):
            lw.config_overrides(remote, 32768)


class Pins(unittest.TestCase):
    def setUp(self):
        self.profile = sp.discovered_profile()

    def argv(self, window=32768, profile=None, **kwargs):
        return lw.worker_argv("codex", Path("/wt"), profile or self.profile, window,
                              Path("/r/l.txt"), **kwargs)

    def test_worker_argv_pins_the_profile_and_passes_its_self_check(self):
        profile = omlx_discovery.discover(sp.write_settings(sp.tmpdir() / "settings.json"),
                                          fetch=sp.FakeOmlx("one"))
        argv = lw.worker_argv("codex", Path("/wt"), profile, 32768, Path("/r/l.txt"))
        pins = lw.config_overrides_map(argv)
        self.assertEqual(pins["model"], '"Qwen3.5-9B-OptiQ-4bit"')
        self.assertEqual(pins["model_context_window"], "32768")
        self.assertEqual(pins["model_auto_compact_token_limit"], str(int(32768 * 0.8)))
        self.assertEqual(lw.missing_config_pins(argv, profile, 32768), [])
        self.assertTrue(lw.missing_config_pins(argv, profile, 65536))

    def test_every_expected_value_comes_from_the_profile(self):
        argv = self.argv()
        pins = lw.config_overrides_map(argv)
        provider = "model_providers.%s" % lw.PROVIDER_ID
        self.assertEqual(argv[argv.index("-m") + 1], MODEL)
        self.assertEqual(pins[provider + ".base_url"], '"%s/v1"' % self.profile.base_url)
        self.assertEqual(lw.COMPACT_FRACTION, 0.8)
        for window in (4096, 32768, 262144, 1048576):
            with self.subTest(window=window):
                pins = lw.config_overrides_map(self.argv(window))
                self.assertEqual(pins["model_context_window"], str(window))
                self.assertEqual(pins["model_auto_compact_token_limit"],
                                 str(int(window * lw.COMPACT_FRACTION)))
                self.assertEqual(lw.missing_config_pins(self.argv(window), self.profile, window),
                                 [])

    def test_an_aliased_model_is_pinned_under_its_alias(self):
        profile = sp.discovered_profile(overrides={"model_alias": "nine-b"})
        argv = self.argv(profile=profile)
        self.assertEqual(argv[argv.index("-m") + 1], "nine-b")
        self.assertEqual(lw.config_overrides_map(argv)["model"], '"nine-b"')
        self.assertEqual(lw.missing_config_pins(argv, profile, 32768), [])
        self.assertTrue(lw.missing_config_pins(argv, self.profile, 32768))

    def test_the_static_floor_holds_no_model_fact(self):
        for key in ("model", "model_context_window", "model_auto_compact_token_limit",
                    "model_providers.%s.base_url" % lw.PROVIDER_ID):
            self.assertNotIn(key, lw.CONFIG_PINS)

    def test_drift_from_the_pinned_profile_is_refused(self):
        argv = self.argv()
        endpoint = lw.profile_endpoint(self.profile)
        for label, drifted in [
                ("other base_url", [i.replace(endpoint, "https://api.openai.com/v1")
                                    for i in argv]),
                ("other loopback port", [i.replace(endpoint, "http://127.0.0.1:9999/v1")
                                         for i in argv]),
                ("other loopback host", [i.replace(endpoint, "http://localhost:8000/v1")
                                         for i in argv]),
                ("other model", [i.replace(MODEL, "other-model") for i in argv]),
                ("no compaction pin", [i for i in argv if not i.startswith(
                    "model_auto_compact_token_limit=")]),
                ("other compaction", [i.replace("model_auto_compact_token_limit=26214",
                                                "model_auto_compact_token_limit=999")
                                      for i in argv]),
                ("no window pin", [i for i in argv if not i.startswith("model_context_window=")])]:
            with self.subTest(label=label):
                self.assertTrue(lw.missing_config_pins(drifted, self.profile, 32768))
                with self.assertRaises(lw.BlockedError):
                    lw._policy_check(drifted, self.profile, 32768)

    def test_a_second_conflicting_model_flag_is_refused(self):
        argv = self.argv()
        conflicting = argv[:-2] + ["--model", "other-model"] + argv[-2:]
        with self.assertRaises(lw.BlockedError):
            lw._policy_check(conflicting, self.profile, 32768)

    def test_a_base_url_pin_naming_a_loopback_host_other_than_the_profiles_is_refused(self):
        # Any loopback URL passes the shape check; it must also be the pinned profile's own.
        argv = self.argv()
        for other in ("http://localhost:8000/v1", "http://[::1]:8000/v1"):
            drifted = [i.replace(lw.profile_endpoint(self.profile), other) for i in argv]
            with self.subTest(other=other):
                problems = " ".join(lw.missing_config_pins(drifted, self.profile, 32768))
                self.assertIn("base_url", problems)

    def test_an_ipv6_loopback_profile_pins_and_self_checks(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        settings = sp.write_settings(root / "settings.json", host="::1", port=8123)
        profile = omlx_discovery.discover(settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(lw.profile_endpoint(profile), "http://[::1]:8123/v1")
        argv = lw.worker_argv("codex", Path("/wt"), profile, 32768, Path("/r/l.txt"))
        self.assertEqual(lw.missing_config_pins(argv, profile, 32768), [])

    def test_a_catalog_path_adds_exactly_the_catalog_pin(self):
        plain = self.argv()
        with_catalog = self.argv(catalog_path=Path("/c/catalog.json"))
        self.assertNotIn("model_catalog_json", lw.config_overrides_map(plain))
        added = [pair for pair in lw.config_override_pairs(with_catalog)
                 if pair not in lw.config_override_pairs(plain)]
        self.assertEqual(added, [("model_catalog_json", '"/c/catalog.json"')])
        self.assertEqual(len(with_catalog), len(plain) + 2)


class JobReport(LauncherCase):
    def test_the_report_names_the_discovered_model_and_the_worker_is_pinned_to_it(self):
        report, fake = self.run_clean_job()
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["endpoint"], "http://127.0.0.1:8000/v1")
        self.assertEqual(report["api_model"], MODEL)
        self.assertEqual(report["context_window"], 262144)
        self.assertEqual(report["model_profile"]["model"]["canonical_id"], MODEL)
        self.assertEqual(report["model_profile"]["base_url"], "http://127.0.0.1:8000")
        self.assertEqual(report["preflight"]["profile"], report["model_profile"])
        self.assertEqual(report["preflight"]["context_window"], report["context_window"])
        self.assertIs(report["model_fallback"], False)
        self.assertEqual(report["attempts"][0]["model_routing"], "ok")
        worker = fake.calls_of("exec")[0]
        self.assertEqual(worker["argv"][worker["argv"].index("-m") + 1], MODEL)
        pins = lw.config_overrides_map(worker["argv"])
        self.assertEqual(pins["model_context_window"], "262144")
        self.assertEqual(pins["model_auto_compact_token_limit"], str(int(262144 * 0.8)))
        for old in ("canonical_model", "model_context_window"):
            self.assertNotIn(old, report)
        persisted = Path(report["report_dir"], "report.json").read_text(encoding="utf-8")
        self.assertNotIn(sp.SECRET, persisted)
        self.assertEqual(json.loads(persisted)["model_profile"], report["model_profile"])

    def test_the_options_carry_a_context_cap_that_lowers_the_pinned_window(self):
        options = sp.job_options(self.root, self.repo)
        options.context_cap = 32768
        fake = sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["context_window"], 32768)
        self.assertEqual(report["model_profile"]["model"]["context_window"], 262144)
        pins = lw.config_overrides_map(fake.calls_of("exec")[0]["argv"])
        self.assertEqual(pins["model_context_window"], "32768")
        self.assertEqual(pins["model_auto_compact_token_limit"], str(int(32768 * 0.8)))
        self.assertIn("context 32768", "\n".join(lw.summary_lines(report)))

    def test_a_requested_model_is_the_one_the_job_runs_on(self):
        options = sp.job_options(self.root / "named", self.repo, omlx=sp.FakeOmlx("two_loaded"))
        options.requested_model = "Example-Text-7B-4bit"
        fake = sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["api_model"], "Example-Text-7B-4bit")
        self.assertEqual(report["context_window"], 32768)
        argv = fake.calls_of("exec")[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "Example-Text-7B-4bit")

    def test_a_worker_served_by_another_model_is_still_a_mismatch(self):
        report, _ = self.run_clean_job(fake_run=sp.FakeRun(worker_edit=sp.fix_add,
                                                           routed_model="gpt-oss-120b"))
        self.assertEqual(report["status"], "model_mismatch")
        self.assertEqual(report["attempts"][0]["model_routing"], "mismatch")
        self.assertIn(MODEL, report["failure_reason"])

    def test_the_summary_names_the_model_endpoint_and_window(self):
        report, _ = self.run_clean_job()
        line = [item for item in lw.summary_lines(report) if item.startswith("model:")][0]
        self.assertIn("%s -> %s @ http://127.0.0.1:8000/v1" % (MODEL, MODEL), line)
        self.assertIn("context 262144", line)

    def test_the_summary_of_a_refused_job_does_not_invent_a_model(self):
        report = self.run_job(omlx=sp.FakeOmlx("none_loaded"))
        line = [item for item in lw.summary_lines(report) if item.startswith("model:")][0]
        self.assertNotIn("None", line)

    def test_discovery_error_maps_to_preflight_failed_with_code(self):
        report = self.run_job(omlx=sp.FakeOmlx("two_loaded"))
        self.assertEqual(report["status"], "preflight_failed")
        self.assertEqual(report["discovery_error"]["code"], "model_ambiguous")

    def test_the_discovery_error_has_code_message_and_candidates_and_nothing_launches(self):
        report, fake = self.run_clean_job(omlx=sp.FakeOmlx("two_loaded"))
        error = report["discovery_error"]
        self.assertEqual(sorted(error), ["candidates", "code", "message"])
        self.assertEqual(sorted(error["candidates"]), sorted(["Example-Text-7B-4bit", MODEL]))
        self.assertIn("name one explicitly", error["message"])
        self.assertEqual(report["failure_reason"], error["message"])
        self.assertIsNone(report["worktree"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(report["exit_code"], lw.EXIT_CODES["preflight_failed"])
        persisted = json.loads(Path(report["report_dir"], "report.json").read_text("utf-8"))
        self.assertEqual(persisted["discovery_error"], error)
        self.assertNotIn(sp.SECRET, json.dumps(persisted))

    def test_not_loaded_lists_the_models_that_could_be_loaded(self):
        report = self.run_job(omlx=sp.FakeOmlx("none_loaded"))
        self.assertEqual(report["status"], "preflight_failed")
        self.assertEqual(report["discovery_error"]["code"], "model_not_loaded")
        self.assertEqual(report["discovery_error"]["candidates"], [MODEL])

    def test_a_successful_job_has_no_discovery_error(self):
        report = self.run_job()
        self.assertNotIn("discovery_error", report)

    def test_a_settings_problem_is_reported_with_its_code_too(self):
        options = sp.job_options(self.root, self.repo)
        options.settings_path.unlink()
        report = lw.run_job(options)
        self.assertEqual(report["status"], "preflight_failed")
        self.assertEqual(report["discovery_error"]["code"], "settings_invalid")

    def test_job_options_use_discovery_fields_only(self):
        fields = set(lw.JobOptions.__dataclass_fields__)
        self.assertLessEqual({"requested_model", "context_cap", "omlx_fetch", "load_model"}, fields)
        self.assertNotIn("attempt_timeout_cap", fields)
        for gone in ("list_models", "canonical_model", "api_model", "context_window",
                     "model_settings_path"):
            self.assertNotIn(gone, fields)
        options = sp.job_options(self.root, self.repo)
        self.assertIsNone(options.requested_model)
        self.assertIsNone(options.context_cap)
        self.assertFalse(options.load_model)
        self.assertIs(lw.JobOptions.__dataclass_fields__["omlx_fetch"].default,
                      omlx_discovery.http_fetch)


def refusing_after(fake, count):
    """``fake`` for its first ``count`` requests, then a server that refuses every connection."""
    def fetch(settings, path, auth):
        if len(fake.calls) >= count:
            raise omlx_discovery.DiscoveryError(
                "server_unreachable", "cannot reach oMLX at %s (ConnectionRefusedError); is it "
                "running?" % settings.base_url)
        return fake(settings, path, auth)
    return fetch


class AttemptRecheck(LauncherCase):
    """Before every attempt the launcher re-reads oMLX; a swapped model never mixes results.

    Preflight discovery makes three requests (``/health``, ``/v1/models/status``,
    ``/api/status``) and each recheck one, so ``after_calls(3, ...)`` swaps the server right
    after preflight and ``after_calls(4, ...)`` right after the recheck before attempt 1.
    """

    def unfixed_run(self, **kwargs):
        """A worker that edits nothing and a verification that fails, so a retry follows."""
        return sp.FakeRun(worker_edit=lambda cwd, attempt: None, test_rc=1, **kwargs)

    def test_a_model_unloaded_before_attempt_one_blocks_it(self):
        fake = sp.FakeOmlx("one").after_calls(3, variant="none_loaded")
        run = sp.FakeRun(worker_edit=sp.fix_add)
        report = self.run_job(omlx=fake, fake_run=run)
        self.assertEqual(run.exec_calls, 0)
        self.assertEqual(run.calls_of("exec"), [])
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(report["attempts"][0]["attempt"], 1)
        self.assertEqual(report["attempts"][0]["skipped_reason"], "model_changed")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["exit_code"], lw.EXIT_CODES["blocked"])
        self.assertIn(MODEL, report["failure_reason"])
        self.assertIn("no chat model is loaded", report["failure_reason"])
        self.assertEqual(report["failure_reason"], report["attempts"][0]["skipped"])
        self.assertEqual(report["patch_bytes"], 0)
        self.assertIs(report["primary_unchanged"], True)

    def test_a_swapped_in_model_is_named_next_to_the_pinned_one(self):
        fake = sp.FakeOmlx("two_loaded").after_calls(3, variant="one")
        options = sp.job_options(self.root / "named", self.repo, omlx=fake)
        options.requested_model = "Example-Text-7B-4bit"
        run = sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(lw, "run_bounded", run):
            report = lw.run_job(options)
        self.assertEqual(report["api_model"], "Example-Text-7B-4bit")
        self.assertEqual(run.exec_calls, 0)
        self.assertEqual(report["attempts"][0]["skipped_reason"], "model_changed")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("Example-Text-7B-4bit", report["failure_reason"])
        self.assertIn(MODEL, report["failure_reason"])

    def test_a_context_window_that_shrinks_blocks_the_attempt_and_says_so(self):
        fake = sp.FakeOmlx("one").after_calls(3, overrides={"max_context_window": 32768})
        run = sp.FakeRun(worker_edit=sp.fix_add)
        report = self.run_job(omlx=fake, fake_run=run)
        self.assertEqual(run.exec_calls, 0)
        self.assertEqual(report["attempts"][0]["skipped_reason"], "model_changed")
        self.assertEqual(report["status"], "blocked")
        self.assertIn(MODEL, report["failure_reason"])
        self.assertIn("262144", report["failure_reason"])
        self.assertIn("32768", report["failure_reason"])

    def test_what_the_server_reports_now_is_put_in_words(self):
        pinned = sp.discovered_profile("one")
        for variant, expected in (("none_loaded", "no chat model is loaded"),
                                  ("two_loaded", "several chat models are loaded")):
            with self.subTest(variant=variant):
                words = lw.describe_model_change(pinned, self.settings, sp.FakeOmlx(variant))
                self.assertIn(expected, words)
        listed = lw.describe_model_change(pinned, self.settings, sp.FakeOmlx("two_loaded"))
        self.assertIn(MODEL, listed)
        self.assertIn("Example-Text-7B-4bit", listed)
        on_the_small_model = sp.discovered_profile("two_loaded", requested="Example-Text-7B-4bit")
        swapped = lw.describe_model_change(on_the_small_model, self.settings, sp.FakeOmlx("one"))
        self.assertIn("now serves %s" % MODEL, swapped)

    def test_a_server_that_refuses_discovery_is_described_by_its_code(self):
        pinned = sp.discovered_profile("one")
        sp.write_settings(self.settings, model_fallback=True)
        words = lw.describe_model_change(pinned, self.settings, sp.FakeOmlx("one"))
        self.assertIn("model_fallback_enabled", words)

    def test_a_name_the_server_reports_is_quoted_safely(self):
        self.assertEqual(lw.model_label(MODEL), MODEL)
        shown = lw.model_label("evil\nmodel " + "x" * 200)
        self.assertNotIn("\n", shown)
        self.assertLess(len(shown), 100)

    def test_an_unreachable_server_blocks_the_attempt(self):
        fetch = refusing_after(sp.FakeOmlx("one"), 3)
        run = sp.FakeRun(worker_edit=sp.fix_add)
        report = self.run_job(omlx=fetch, fake_run=run)
        self.assertEqual(run.exec_calls, 0)
        self.assertEqual(report["attempts"][0]["skipped_reason"], "server_unreachable")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["exit_code"], lw.EXIT_CODES["blocked"])
        self.assertIn(MODEL, report["failure_reason"])
        self.assertIn("unreachable", report["failure_reason"])
        self.assertIn("http://127.0.0.1:8000", report["failure_reason"])
        self.assertNotIn(sp.SECRET, json.dumps(report))

    def test_a_swap_before_attempt_two_skips_only_the_second_attempt(self):
        # Preflight (3 requests) + the recheck before attempt 1 = 4: attempt 2 sees the swap.
        fake = sp.FakeOmlx("one").after_calls(4, variant="none_loaded")
        run = sp.FakeRun(worker_edit=sp.fix_add, test_rc=1)
        report, _ = self.run_clean_job(omlx=fake, fake_run=run, max_attempts=2)
        self.assertEqual(run.exec_calls, 1)
        first, second = report["attempts"]
        self.assertEqual(first["attempt"], 1)
        self.assertNotIn("skipped_reason", first)
        self.assertEqual(first["returncode"], 0)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["skipped_reason"], "model_changed")
        self.assertEqual(report["status"], "blocked")
        self.assertIn(MODEL, report["failure_reason"])
        # What attempt 1 left behind is still audited and preserved, not thrown away.
        self.assertEqual(report["changed_paths"], ["add.py"])
        self.assertGreater(report["patch_bytes"], 0)
        self.assertTrue(Path(report["patch_path"]).is_file())
        self.assertIs(report["primary_unchanged"], True)

    def test_an_unreachable_server_before_attempt_two_skips_only_the_second_attempt(self):
        fetch = refusing_after(sp.FakeOmlx("one"), 4)
        run = sp.FakeRun(worker_edit=sp.fix_add, test_rc=1)
        report, _ = self.run_clean_job(omlx=fetch, fake_run=run, max_attempts=2)
        self.assertEqual(run.exec_calls, 1)
        self.assertEqual(report["attempts"][1]["skipped_reason"], "server_unreachable")
        self.assertEqual(report["status"], "blocked")

    def test_a_steady_server_is_rechecked_once_per_attempt_and_nothing_is_skipped(self):
        one = sp.FakeOmlx("one")
        report = self.run_job(omlx=one)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(one.calls, ["/health", "/v1/models/status", "/api/status",
                                     "/v1/models/status"])
        two = sp.FakeOmlx("one")
        report, run = self.run_clean_job(omlx=two, fake_run=self.unfixed_run(), max_attempts=2)
        self.assertEqual(report["status"], "tests_failed")
        self.assertEqual(run.exec_calls, 2)
        self.assertEqual(two.calls, ["/health", "/v1/models/status", "/api/status",
                                     "/v1/models/status", "/v1/models/status"])
        for attempt in report["attempts"]:
            self.assertNotIn("skipped_reason", attempt)

    def test_the_blocked_report_is_persisted_with_the_skip_and_never_holds_the_key(self):
        fake = sp.FakeOmlx("one").after_calls(3, variant="none_loaded")
        report = self.run_job(omlx=fake)
        persisted = Path(report["report_dir"], "report.json").read_text(encoding="utf-8")
        self.assertNotIn(sp.SECRET, persisted)
        saved = json.loads(persisted)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["attempts"][0]["skipped_reason"], "model_changed")
        self.assertEqual(saved["failure_reason"], report["failure_reason"])


class Cli(LauncherCase):
    def test_preflight_failure_prints_the_error_with_its_code_and_exits_2(self):
        code, out = self.cli(["preflight", "--settings", str(self.settings)],
                             sp.FakeOmlx("two_loaded"))
        self.assertEqual(code, lw.EXIT_CODES["preflight_failed"])
        self.assertEqual(out["status"], "preflight_failed")
        self.assertEqual(out["discovery_error"]["code"], "model_ambiguous")
        self.assertEqual(sorted(out["discovery_error"]["candidates"]),
                         sorted(["Example-Text-7B-4bit", MODEL]))

    def test_preflight_model_names_the_loaded_model_to_use(self):
        code, receipt = self.cli(["preflight", "--settings", str(self.settings), "--model",
                                  MODEL], sp.FakeOmlx("two_loaded"))
        self.assertEqual(code, 0)
        self.assertEqual(receipt["profile"]["model"]["canonical_id"], MODEL)

    def test_preflight_makes_one_discovery_and_never_loads_a_model(self):
        fake = sp.FakeOmlx("none_loaded")
        code, out = self.cli(["preflight", "--settings", str(self.settings), "--model", MODEL],
                             fake)
        self.assertEqual(code, lw.EXIT_CODES["preflight_failed"])
        self.assertEqual(out["discovery_error"]["code"], "model_not_loaded")
        self.assertFalse([path for path in fake.calls if path.endswith("/load")])

    def test_run_takes_model_and_context_cap(self):
        code, report, fake = self.cli_run("--model", MODEL, "--context-cap", "32768")
        self.assertEqual(code, 0, report.get("failure_reason"))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["api_model"], MODEL)
        self.assertEqual(report["context_window"], 32768)
        pins = lw.config_overrides_map(fake.calls_of("exec")[0]["argv"])
        self.assertEqual(pins["model_context_window"], "32768")

    def test_run_refuses_a_model_that_is_not_loaded_and_says_how_to_load_it(self):
        code, report, fake = self.cli_run("--model", MODEL, omlx=sp.FakeOmlx("none_loaded"))
        self.assertEqual(code, lw.EXIT_CODES["preflight_failed"])
        self.assertEqual(report["discovery_error"]["code"], "model_not_loaded")
        self.assertIn("--load", report["discovery_error"]["message"])
        self.assertEqual(fake.calls, [])

    def test_load_requires_a_model(self):
        fake = sp.FakeOmlx("none_loaded")
        code, out = self.cli(self.run_args("--load"), fake)
        self.assertEqual(code, lw.EXIT_CODES["contract_invalid"])
        self.assertEqual(out["status"], "contract_invalid")
        self.assertIn("--load requires --model", out["failure_reason"])
        self.assertEqual(fake.calls, [])

    def test_load_loads_the_named_model_before_discovery_and_runs_on_it(self):
        fake = sp.FakeOmlx("none_loaded")
        code, report, worker = self.cli_run("--load", "--model", MODEL, omlx=fake)
        self.assertEqual(code, 0, report.get("failure_reason"))
        self.assertEqual(report["api_model"], MODEL)
        load = "/v1/models/%s/load" % MODEL
        self.assertIn(load, fake.calls)
        self.assertLess(fake.calls.index(load), fake.calls.index("/health"))
        self.assertEqual(fake.calls.count(load), 1)
        self.assertEqual(report["model_load"], {"requested": MODEL, "loaded": True})
        argv = worker.calls_of("exec")[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], MODEL)

    def test_load_failure_is_a_preflight_failure_with_the_discovery_code(self):
        fake = sp.FakeOmlx("none_loaded")
        code, report, worker = self.cli_run("--load", "--model", "No-Such-Model", omlx=fake)
        self.assertEqual(code, lw.EXIT_CODES["preflight_failed"])
        self.assertEqual(report["discovery_error"]["code"], "model_unknown")
        self.assertEqual(worker.calls, [])
        self.assertNotIn("/health", fake.calls)

    def test_without_load_nothing_is_ever_loaded(self):
        fake = sp.FakeOmlx("one")
        code, report, _ = self.cli_run("--model", MODEL, omlx=fake)
        self.assertEqual(code, 0)
        self.assertFalse([path for path in fake.calls if path.endswith("/load")])
        self.assertNotIn("model_load", report)

    def test_the_key_is_never_printed(self):
        fake = sp.FakeOmlx("none_loaded")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                lw, "run_bounded", sp.FakeRun(worker_edit=sp.fix_add)):
            lw.main(self.run_args("--load", "--model", MODEL), omlx_fetch=fake,
                    codex_run=sp.fake_help())
        self.assertNotIn(sp.SECRET, out.getvalue())


class NoModelFactsInTheLauncher(unittest.TestCase):
    def test_no_model_constants_remain(self):
        source = Path(lw.__file__).read_text()
        for token in ("Qwen", "qwen-3.8", "65536", "CANONICAL_MODEL", "EXPECTED_ALIAS"):
            self.assertNotIn(token, source)

    def test_no_port_or_model_setting_name_is_hardcoded_either(self):
        source = Path(lw.__file__).read_text()
        for token in ("8000", "127.0.0.1:", "LOCAL_WORKER_CANONICAL_MODEL",
                      "LOCAL_WORKER_API_MODEL", "LOCAL_WORKER_CONTEXT_WINDOW",
                      "LOCAL_WORKER_ATTEMPT_SECONDS", "model_settings.json"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
