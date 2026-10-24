"""Configurable model routing: CLI > environment > default, pinned and self-checked as strictly
as the defaults. No model or server: the model list is a stub and the worker is ``FakeRun``."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
OTHER = "Qwen3.5-9B-OptiQ-4bit"
FIX = "def add(a, b):\n    return a + b\n"
SETTING_ENVS = (lw.CANONICAL_MODEL_ENV, lw.API_MODEL_ENV, lw.CONTEXT_WINDOW_ENV,
                lw.ATTEMPT_SECONDS_ENV)


@contextlib.contextmanager
def setting_env(**values):
    """os.environ with every model setting variable removed, then *values* applied."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in SETTING_ENVS:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield


def parse(*argv):
    return lw.build_parser().parse_args(["run", "--task-file", "/tmp/task.json", *argv])


def write_unaliased(root, model=OTHER, entry=True):
    """oMLX settings for a model with no alias, as oMLX writes them for a plain download."""
    settings, model_settings = sp.write_legacy_settings(root)
    models = {model: {"temperature": 1.0}} if entry else {}
    model_settings.write_text(json.dumps({"version": 1, "models": models}))
    return settings, model_settings


class Resolution(unittest.TestCase):
    def test_defaults_are_the_original_pins(self):
        with setting_env():
            settings = lw.resolve_model_settings(parse())
        self.assertEqual(settings, {"canonical_model": lw.CANONICAL_MODEL,
                                    "api_model": lw.EXPECTED_ALIAS,
                                    "context_window": lw.MODEL_CONTEXT_WINDOW,
                                    "attempt_seconds": lw.policy.ATTEMPT_SECONDS})

    def test_environment_overrides_defaults_and_cli_overrides_environment(self):
        env = {lw.CANONICAL_MODEL_ENV: "env-model", lw.API_MODEL_ENV: "env-alias",
               lw.CONTEXT_WINDOW_ENV: "16384", lw.ATTEMPT_SECONDS_ENV: "90"}
        with setting_env(**env):
            self.assertEqual(lw.resolve_model_settings(parse()),
                             {"canonical_model": "env-model", "api_model": "env-alias",
                              "context_window": 16384, "attempt_seconds": 90.0})
            cli = lw.resolve_model_settings(parse(
                "--canonical-model", OTHER, "--api-model", OTHER, "--context-window", "32768",
                "--attempt-timeout", "900"))
        self.assertEqual(cli, {"canonical_model": OTHER, "api_model": OTHER,
                               "context_window": 32768, "attempt_seconds": 900.0})

    def test_preflight_takes_model_flags_but_no_attempt_cap(self):
        args = lw.build_parser().parse_args(["preflight", "--api-model", OTHER])
        with setting_env():
            settings = lw.resolve_model_settings(args)
        self.assertEqual(settings["api_model"], OTHER)
        self.assertNotIn("attempt_seconds", settings)

    def test_invalid_values_are_refused(self):
        cases = [("--canonical-model", 'bad"name'), ("--api-model", "has space"),
                 ("--api-model", "-flag"), ("--context-window", "abc"),
                 ("--context-window", "100"), ("--context-window", "99999999"),
                 ("--attempt-timeout", "0"), ("--attempt-timeout", "nan"),
                 ("--attempt-timeout", "inf"), ("--attempt-timeout", "5000")]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value), setting_env(), \
                    self.assertRaises(lw.ContractError):
                lw.resolve_model_settings(parse(f"{flag}={value}"))
        with setting_env(**{lw.CONTEXT_WINDOW_ENV: "lots"}), self.assertRaises(lw.ContractError):
            lw.resolve_model_settings(parse())
        for value in (True, 1.5, None):
            with self.subTest(value=value), self.assertRaises(lw.ContractError):
                lw.validate_context_window(value)
        with self.assertRaises(lw.ContractError):
            lw.validate_attempt_seconds(True)

    def test_cli_reports_an_invalid_setting_as_contract_invalid(self):
        out = io.StringIO()
        with setting_env(), contextlib.redirect_stdout(out):
            code = lw.main(["preflight", "--context-window", "12"],
                           list_models=sp.models_available)
        self.assertEqual(code, lw.EXIT_CODES["contract_invalid"])
        self.assertIn("context window", json.loads(out.getvalue())["failure_reason"])


class ConfiguredPins(unittest.TestCase):
    def argv(self, alias=OTHER, context_window=32768):
        return lw.worker_argv("codex", Path("/wt"), alias, Path("/r/last.txt"),
                              context_window=context_window)

    def test_configured_argv_passes_its_own_self_check(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("-m") + 1], OTHER)
        overrides = lw.config_overrides_map(argv)
        self.assertEqual(overrides["model"], f'"{OTHER}"')
        self.assertEqual(overrides["model_context_window"], "32768")
        self.assertEqual(lw.missing_config_pins(argv, OTHER, 32768), [])
        lw._policy_check(argv, OTHER, 32768)

    def test_drift_from_the_configured_values_is_still_refused(self):
        argv = self.argv()
        against_defaults = " ".join(lw.missing_config_pins(argv))
        self.assertIn("model_context_window", against_defaults)
        self.assertIn(lw.EXPECTED_ALIAS, against_defaults)
        self.assertTrue(lw.missing_config_pins(self.argv(context_window=65536), OTHER, 32768))
        self.assertTrue(lw.missing_config_pins(self.argv(alias="other-model"), OTHER, 32768))
        conflicting = argv[:-2] + ["--model", lw.EXPECTED_ALIAS] + argv[-2:]
        with self.assertRaises(lw.BlockedError):
            lw._policy_check(conflicting, OTHER, 32768)

    def test_default_argv_is_unchanged(self):
        self.assertEqual(lw.worker_argv("codex", Path("/wt"), lw.EXPECTED_ALIAS,
                                        Path("/r/last.txt")),
                         self.argv(lw.EXPECTED_ALIAS, lw.MODEL_CONTEXT_WINDOW))
        self.assertEqual(lw.missing_config_pins(self.argv(lw.EXPECTED_ALIAS,
                                                          lw.MODEL_CONTEXT_WINDOW)), [])


class UnaliasedModels(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)

    def preflight(self, settings, model_settings, canonical=OTHER, api=OTHER, served=(OTHER,),
                  context_window=lw.MODEL_CONTEXT_WINDOW):
        return lw.run_preflight(settings_path=settings, model_settings_path=model_settings,
                                canonical_model=canonical, expected_alias=api,
                                context_window=context_window,
                                list_models=lambda endpoint, key: list(served))

    def test_model_served_under_its_own_id_is_accepted_only_when_asked_for(self):
        for entry in (True, False):
            with self.subTest(entry=entry):
                settings, model_settings = write_unaliased(self.root, entry=entry)
                evidence = self.preflight(settings, model_settings)
                self.assertEqual((evidence["canonical_model"], evidence["api_model"]),
                                 (OTHER, OTHER))
        settings, model_settings = write_unaliased(self.root)
        with self.assertRaises(lw.PreflightError) as ctx:      # an alias is still never guessed
            self.preflight(settings, model_settings, api="some-alias")
        self.assertIn("--api-model", str(ctx.exception))
        with self.assertRaises(lw.PreflightError):             # served list stays authoritative
            self.preflight(settings, model_settings, served=("something-else",))

    def test_an_alias_in_omlx_wins_over_the_bare_id(self):
        settings, model_settings = write_unaliased(self.root)
        model_settings.write_text(json.dumps({"models": {OTHER: {"model_alias": "nine-b"}}}))
        with self.assertRaises(lw.PreflightError) as ctx:
            self.preflight(settings, model_settings)
        self.assertIn("nine-b", str(ctx.exception))
        self.assertEqual(self.preflight(settings, model_settings, api="nine-b",
                                        served=("nine-b",))["api_model"], "nine-b")

    def test_default_model_missing_from_omlx_names_the_way_out(self):
        settings, model_settings = write_unaliased(self.root)
        with self.assertRaises(lw.PreflightError) as ctx:
            lw.run_preflight(settings_path=settings, model_settings_path=model_settings,
                             list_models=lambda endpoint, key: [OTHER])
        self.assertIn("--canonical-model", str(ctx.exception))


class ModelFakeRun(sp.FakeRun):
    """FakeRun whose worker stream reports *model*, the way a configured job is routed."""

    def __init__(self, model, **kwargs):
        super().__init__(**kwargs)
        self.model = model

    def __call__(self, argv, *, cwd, env, timeout, stdin_data=None):
        outcome = super().__call__(argv, cwd=cwd, env=env, timeout=timeout,
                                   stdin_data=stdin_data)
        if [str(item) for item in argv][1:2] == ["exec"]:
            stream = json.dumps({"type": "turn.completed", "model": self.model,
                                 "usage": {"input_tokens": 11, "output_tokens": 7}})
            outcome["stdout"] = (stream + "\n").encode()
        return outcome


class ConfiguredJob(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        settings, model_settings = write_unaliased(Path(self.root) / "omlx")
        self.options = lw.JobOptions(
            task=lw.Task.from_payload(sp.task_payload(self.repo)),
            state_dir=Path(self.root) / "state", codex_bin="codex-mock",
            settings_path=settings, model_settings_path=model_settings,
            list_models=lambda endpoint, key: [OTHER], canonical_model=OTHER, api_model=OTHER,
            context_window=32768, attempt_timeout_cap=42.0)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX)

    def test_job_runs_and_reports_the_configured_model(self):
        fake = ModelFakeRun(OTHER, worker_edit=self.fix)
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(self.options)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual((report["canonical_model"], report["api_model"]), (OTHER, OTHER))
        self.assertEqual(report["model_context_window"], 32768)
        self.assertEqual(report["attempt_timeout_cap_seconds"], 42.0)
        self.assertEqual(report["attempts"][0]["model_routing"], "ok")
        worker = fake.calls_of("exec")[0]
        self.assertEqual(worker["argv"][worker["argv"].index("-m") + 1], OTHER)
        self.assertEqual(lw.config_overrides_map(worker["argv"])["model_context_window"], "32768")
        self.assertLessEqual(worker["timeout"], 42.0)
        self.assertIn("per-attempt cap 42.0s", worker["stdin"])
        self.assertIn("context 32768", "\n".join(lw.summary_lines(report)))

    def test_a_worker_routed_to_another_model_is_still_a_mismatch(self):
        fake = ModelFakeRun(lw.EXPECTED_ALIAS, worker_edit=self.fix)
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(self.options)
        self.assertEqual(report["status"], "model_mismatch")


if __name__ == "__main__":
    unittest.main()
