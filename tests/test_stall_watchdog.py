"""Stall watchdog timing and profile-derived limits."""
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_worker as lw  # noqa: E402
import run_policy as policy  # noqa: E402
import worker_observability as obs  # noqa: E402
import omlx_discovery as discovery  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402


class StallWatchdog(unittest.TestCase):
    def test_quiet_worker_is_stopped_as_stall(self):
        result = obs.run_streaming(["/bin/sh", "-c", "echo '{}'; sleep 30"], cwd=None,
                                   env=os.environ.copy(), timeout=60, stdin_data=b"",
                                   stall_seconds=1.0)
        self.assertTrue(result["stalled"])
        self.assertFalse(result["timed_out"])

    def test_stall_extends_while_server_busy(self):
        calls = []
        result = obs.run_streaming(["/bin/sh", "-c", "sleep 1.5; echo '{}'"], cwd=None,
                                   env=os.environ.copy(), timeout=60, stdin_data=b"",
                                   stall_seconds=1.0,
                                   on_stall=lambda: calls.append(1) or len(calls) == 1)
        self.assertFalse(result["stalled"])
        self.assertEqual(result["returncode"], 0)

    def profile_with(self, prefill, generation):
        model = discovery.ModelProfile("m", "m", "chat", None, 32768, None, False, None, None)
        return discovery.ServerProfile("http://127.0.0.1:8000", None, model,
                                       prefill, generation, None, None, "now")

    def test_limit_uses_measured_rates_or_a_conservative_default(self):
        measured = lw.stall_limit(self.profile_with(500.0, 40.0), 32768)
        self.assertEqual(measured["basis"], "measured")
        self.assertAlmostEqual(measured["seconds"], max(120.0, 1.5 * (32768 * 0.8 / 500.0 + 8192 / 40.0)))
        self.assertEqual(lw.stall_limit(self.profile_with(None, None), 32768)["seconds"], 900.0)

    def test_no_fixed_attempt_cap_remains(self):
        self.assertFalse(hasattr(policy, "ATTEMPT_SECONDS"))
        self.assertFalse(hasattr(lw.JobOptions, "attempt_timeout_cap"))

    def test_load_request_uses_the_remaining_job_timeout(self):
        with mock.patch.object(discovery, "load_model") as load:
            lw.load_requested_model(Path("settings.json"), "model-id", discovery.http_fetch,
                                    timeout=3.25)
        self.assertEqual(load.call_args.kwargs["timeout"], 3.25)

    def test_attempt_report_records_stall_inputs_and_job_budget(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        report, _ = sp.run_clean_job(root, repo)
        limits = report["attempts"][0]["limits"]
        self.assertIn(limits["stall"]["basis"], ("measured", "unknown_rates"))
        self.assertGreaterEqual(limits["stall"]["seconds"], lw.STALL_FLOOR_SECONDS)
        self.assertIn("inputs", limits["stall"])
        self.assertGreater(limits["budget_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
