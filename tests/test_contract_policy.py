"""Task contract validation, environment isolation, argv policy for the discovered model."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
TMP = tempfile.gettempdir()


class Contract(unittest.TestCase):
    def bad(self, **overrides):
        with self.assertRaises(lw.ContractError):
            lw.Task.from_payload(sp.task_payload(TMP, **overrides))

    def test_malformed_payloads_are_rejected(self):
        for value in (None, [], "x", 7, {}):
            with self.assertRaises(lw.ContractError):
                lw.Task.from_payload(value)
        base = sp.task_payload(TMP)
        for field in sorted(base):
            with self.assertRaises(lw.ContractError) as ctx:
                lw.Task.from_payload({k: v for k, v in base.items() if k != field})
            self.assertIn("missing contract field", str(ctx.exception))
        with self.assertRaises(lw.ContractError) as ctx:
            lw.Task.from_payload(dict(base, surprise=1))
        self.assertIn("unknown contract field", str(ctx.exception))
        with self.assertRaises(lw.ContractError):
            lw.Task.from_payload(dict(base, task_id=123))

    def test_field_bounds_and_path_traversal(self):
        for field, value in [("task_id", ""), ("task_id", "../t"), ("task_id", "a/b"),
                             ("task_id", "x" * 65), ("repo_path", "relative"),
                             ("repo_path", "/no/such/dir"), ("base_ref", ""), ("base_ref", "-x"),
                             ("base_ref", "HEAD~1"), ("base_ref", "a..b"), ("goal", "  "),
                             ("acceptance_criteria", []), ("acceptance_criteria", "one"),
                             ("timeout_seconds", "60"), ("timeout_seconds", True),
                             ("timeout_seconds", 29), ("timeout_seconds", 3601),
                             ("max_attempts", 0), ("max_attempts", 3), ("max_attempts", "1"),
                             ("test_commands", []), ("test_commands", "pytest -x"),
                             ("test_commands", ["pytest -x"]), ("test_commands", [[]]),
                             ("test_commands", [["pytest", ""]]), ("test_commands", [None]),
                             ("test_commands", {})]:
            with self.subTest(field=field, value=value):
                self.bad(**{field: value})
        for value in (["../secret"], [".git/config"], ["/etc/passwd"], ["~/x"], ["docs/../../x"],
                      ["C:\\evil"], ["a/.git/b"], [], ["."], [""]):
            with self.subTest(allowed=value), self.assertRaises(lw.ContractError):
                lw.Task.from_payload(sp.task_payload(TMP, allowed_paths=value))

    def test_allowed_paths_are_normalized_and_prefix_matched(self):
        task = lw.Task.from_payload(sp.task_payload(
            TMP, allowed_paths=["./add//a.py", "add/a.py", "pkg/./b.py", "pkg/"]))
        self.assertEqual(task.allowed_paths, ["add/a.py", "pkg/b.py", "pkg"])
        self.assertEqual(task.test_commands, [["python3", "-m", "unittest", "-v"]])
        for path, allowed in (("add/a.py", True), ("add/a.py.bak", False), ("pkg/b.py", True),
                              ("pkg/sub/c.py", True), ("pkgx/b.py", False), ("notes.md", False)):
            with self.subTest(path=path):
                self.assertEqual(lw.is_allowed(path, task.allowed_paths), allowed)


class EnvironmentAndArgv(unittest.TestCase):
    def setUp(self):
        self.profile = sp.discovered_profile()
        self.window = self.profile.model.context_window
        self.argv = lw.worker_argv("codex", Path("/wt"), self.profile, self.window,
                                   Path("/r/last.txt"))
        self.task = lw.Task.from_payload(sp.task_payload(TMP))

    def test_cloud_credentials_and_proxies_never_reach_the_worker(self):
        injected = {"PATH": "/usr/bin", "HOME": "/tmp", "HTTP_PROXY": "http://corp:3128",
                    "no_proxy": "x", "OPENAI_API_KEY": "sk-cloud",
                    "AWS_SECRET_ACCESS_KEY": "aws", "GITHUB_TOKEN": "gh", "SOME_LOCAL_TOOL": "keep"}
        with mock.patch.dict(os.environ, injected, clear=True):
            env, proxies, cloud = lw.worker_env(sp.SECRET)
        self.assertEqual(env[lw.API_KEY_ENV], sp.SECRET)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(sorted(proxies), ["HTTP_PROXY", "no_proxy"])
        self.assertEqual(cloud, ["AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY"])
        for name in list(proxies) + cloud + ["SOME_LOCAL_TOOL"]:
            self.assertNotIn(name, env)
        self.assertNotIn("sk-cloud", json.dumps(env))
        self.assertEqual(lw.redact("key=sk-cloud tail", ["sk-cloud"]), "key=[REDACTED] tail")

    def test_worker_and_sandbox_argv_are_pinned_and_self_checked(self):
        self.assertEqual(lw.missing_config_pins(self.argv, self.profile, self.window), [])
        lw._policy_check(self.argv, self.profile, self.window)
        self.assertIn("--ignore-user-config", self.argv)
        self.assertIn("--ignore-rules", self.argv)
        self.assertEqual(self.argv[self.argv.index("-s") + 1], "danger-full-access")
        self.assertIn("-c", self.argv)
        self.assertIn('sandbox_mode="danger-full-access"', self.argv)
        self.assertEqual(self.argv[self.argv.index("-m") + 1], self.profile.model.api_model)
        self.assertEqual(self.argv[-2:], ["--", "-"])
        self.assertIn(lw.API_KEY_ENV, json.dumps(self.argv))       # key by env name only
        self.assertNotIn(sp.SECRET, json.dumps(self.argv))
        wrapped = lw.sandbox_argv("codex", Path("/wt"), ["python3", "-m", "unittest", "-v"])
        self.assertEqual(wrapped[:2], ["codex", "sandbox"])
        self.assertEqual(wrapped[wrapped.index("-P") + 1], lw.SANDBOX_PROFILE)
        self.assertIn(f"permissions.{lw.SANDBOX_PROFILE}.network.enabled=false", wrapped)
        self.assertEqual(wrapped[wrapped.index("--") + 1:], ["python3", "-m", "unittest", "-v"])
        self.assertNotIn("/bin/sh", wrapped)

    def test_any_policy_drift_is_a_refusal(self):
        endpoint = lw.profile_endpoint(self.profile)
        variants = [[i.replace(endpoint, "https://api.openai.com/v1") for i in self.argv],
                    [i for i in self.argv if not i.endswith("request_max_retries=0")],
                    [i.replace('"never"', '"on-request"') for i in self.argv],
                    [i.replace('"danger-full-access"', '"workspace-write"') for i in self.argv],
                    [i for i in self.argv if not i.startswith("shell_environment_policy.exclude=")],
                    [i.replace('inherit="core"', 'inherit="all"') for i in self.argv]]
        for index, argv in enumerate(variants):
            with self.subTest(variant=index):
                self.assertTrue(lw.missing_config_pins(argv, self.profile, self.window))
                with self.assertRaises(lw.BlockedError):
                    lw._policy_check(argv, self.profile, self.window)

    def test_unavailable_sandbox_and_short_budget_block_never_run_unsandboxed(self):
        for probe_ok, expected in ((True, "passed"), (False, "blocked")):
            outcome = sp.result(["x"], 0 if probe_ok else 1,
                                lw.PROBE_MARKER if probe_ok else "sandbox refused")
            with mock.patch.object(lw, "run_bounded", return_value=outcome) as run:
                probe = lw.probe_sandbox("codex", Path("/wt"), {})
                self.assertEqual(probe["available"], probe_ok)
                self.assertEqual(probe["backend"], lw.FULL_HOST_BACKEND)
                tests = lw.run_verification(self.task, Path("/wt"), {}, "codex", probe, [],
                                            lw.Deadline(120.0))
                self.assertEqual([test["status"] for test in tests], [expected])
                self.assertFalse(tests[0]["sandboxed"])     # full-host never claims confinement
                self.assertEqual(tests[0]["backend"], lw.FULL_HOST_BACKEND)
                self.assertEqual(run.call_count, 2 if probe_ok else 1)   # probe only if blocked
                if probe_ok:
                    self.assertEqual(run.call_args[0][0], list(self.task.test_commands[0]))
                    self.assertEqual(run.call_args.kwargs["env"]["IBToolNeverDeque"], "1")
                else:
                    self.assertIn("full-host", tests[0]["reason"])
        # legacy confinement stays available for a caller that explicitly selects it
        with mock.patch.object(lw, "run_bounded",
                               return_value=sp.result(["x"], 0, lw.PROBE_MARKER)) as run:
            probe = lw.probe_sandbox("codex", Path("/wt"), {},
                                     backend=lw.WORKSPACE_SANDBOX_BACKEND)
            self.assertEqual(probe["backend"], lw.WORKSPACE_SANDBOX_BACKEND)
            tests = lw.run_verification(self.task, Path("/wt"), {}, "codex", probe, [],
                                        lw.Deadline(120.0))
        self.assertEqual(tests[0]["status"], "passed")
        self.assertTrue(tests[0]["sandboxed"])
        self.assertEqual(run.call_args[0][0][:2], ["codex", "sandbox"])
        with mock.patch.object(lw, "run_bounded",
                               return_value=sp.result(["x"], 0, lw.PROBE_MARKER)) as run:
            tests = lw.run_verification(self.task, Path("/wt"), {}, "codex", {"available": True},
                                        [], lw.Deadline(1.0))
        self.assertEqual(tests[0]["status"], "blocked")
        self.assertIn("budget", tests[0]["reason"])
        self.assertEqual(run.call_count, 0)


if __name__ == "__main__":
    unittest.main()
