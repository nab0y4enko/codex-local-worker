"""Regression coverage for the reviewed defects on this patched base.

Six narrow claims, each aimed at the exact reviewed hole: aliased model/sandbox flags are one
setting, the verdict follows the attempt that actually ran last, a terminal failure makes usage
partial even at exit 0, a live snapshot keeps job metadata so a dead launcher is still visible,
an interrupted run kills its process group through the shared runner, and the prompt states the effective
attempt cutoff rather than only the total budget. Nothing here calls a model, a server or a
native tool.
"""
import json
import os
import re
import selectors
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
observation = lw.observation
FIX = "def add(a, b):\n    return a + b\n"


class AliasedFlagsAreOneSetting(unittest.TestCase):
    def test_second_alias_conflict_is_not_missed(self):
        problems = lw._single_valued_flag(["-m", sp.MODEL, "--model", "gpt-5"],
                                          lw.MODEL_FLAGS, sp.MODEL)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("conflicting values", problems[0])

    def test_sandbox_alias_conflict_is_not_missed(self):
        problems = lw._single_valued_flag(["--sandbox", lw.REQUIRED_SANDBOX_MODE,
                                          "-s", "read-only"],
                                          lw.SANDBOX_FLAGS, lw.REQUIRED_SANDBOX_MODE)
        self.assertEqual(len(problems), 1, problems)

    def test_conflict_is_reported_in_either_alias_order(self):
        for argv in (["--model", "gpt-5", "-m", sp.MODEL],
                     ["-s", "read-only", "--sandbox", lw.REQUIRED_SANDBOX_MODE]):
            flags = lw.MODEL_FLAGS if "-m" in argv else lw.SANDBOX_FLAGS
            expected = sp.MODEL if "-m" in argv else lw.REQUIRED_SANDBOX_MODE
            self.assertTrue(lw._single_valued_flag(argv, flags, expected), argv)

    def test_agreeing_aliases_and_a_single_alias_stay_clean(self):
        self.assertEqual(lw._single_valued_flag(["-m", sp.MODEL, "--model",
                                                 sp.MODEL],
                                                lw.MODEL_FLAGS, sp.MODEL), [])
        self.assertEqual(lw._single_valued_flag(["--sandbox", lw.REQUIRED_SANDBOX_MODE],
                                                lw.SANDBOX_FLAGS, lw.REQUIRED_SANDBOX_MODE), [])

    def test_wrong_value_and_absence_still_report(self):
        self.assertIn("--model", lw._single_valued_flag(["--model", "gpt-5"], lw.MODEL_FLAGS,
                                                        sp.MODEL)[0])
        self.assertIn("is missing", lw._single_valued_flag(["-C", "/tmp"], lw.MODEL_FLAGS,
                                                           sp.MODEL)[0])

    def test_the_real_worker_argv_still_passes_the_policy_check(self):
        profile = sp.discovered_profile()
        window = profile.model.context_window
        argv = lw.worker_argv("codex", Path("/tmp/wt"), profile, window, Path("/tmp/last.txt"))
        self.assertEqual(lw.missing_config_pins(argv, profile, window), [])


def attempt(number, *, terminal=(), returncode=0):
    return {"attempt": number, "returncode": returncode, "timed_out": False,
            "model_routing": "match", "unsupported_tools": [],
            "terminal_failures": list(terminal)}


class VerdictFollowsTheLastAttempt(unittest.TestCase):
    def report(self, attempts, statuses=("passed",)):
        return {"attempts": attempts, "tests": [{"status": item} for item in statuses],
                "violations": [], "budget": {}}

    def test_clean_retry_recovers_an_earlier_terminal_failure(self):
        report = self.report([attempt(1, terminal=["turn.failed"]), attempt(2)])
        self.assertEqual(lw.decide_status(report, True), "ok")
        self.assertEqual(report["attempts"][0]["terminal_failures"], ["turn.failed"])

    def test_terminal_failure_on_the_final_attempt_still_fails(self):
        report = self.report([attempt(1), attempt(2, terminal=["response.failed"])])
        self.assertEqual(lw.decide_status(report, True), "worker_failed")

    def test_unrun_attempt_still_needs_verification_evidence(self):
        report = self.report([attempt(1, terminal=["turn.failed"]), attempt(2)],
                             statuses=("failed",))
        self.assertEqual(lw.decide_status(report, True), "tests_failed")


class TerminalRun(sp.FakeRun):
    """A worker that documents a terminal failure and still exits 0."""

    def __call__(self, argv, *, cwd, env, timeout, stdin_data=None):
        inner, _ = sp.deny_wrapped([str(item) for item in argv])
        if inner[1:2] != ["exec"]:
            return super().__call__(argv, cwd=cwd, env=env, timeout=timeout,
                                    stdin_data=stdin_data)
        self.exec_calls += 1
        if self.worker_edit:
            self.worker_edit(Path(cwd), self.exec_calls)
        events = [{"type": "thread.started", "thread_id": "thr-1"},
                  {"type": "turn.completed", "model": sp.worker_model(inner),
                   "usage": {"input_tokens": 11, "output_tokens": 7}},
                  {"type": "turn.failed", "error": {"message": "the turn was aborted"}}]
        return sp.result(list(argv), 0, "".join(json.dumps(e) + "\n" for e in events))


class TerminalUsageIsPartial(unittest.TestCase):
    def test_zero_exit_with_terminal_failure_reports_partial_usage(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        fake = TerminalRun(worker_edit=lambda cwd, attempt=1: Path(cwd, "add.py").write_text(FIX))
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(sp.job_options(root / "job", repo))
        used = report["attempts"][0]["usage"]
        self.assertEqual(report["attempts"][0]["returncode"], 0)
        self.assertEqual(report["attempts"][0]["terminal_failures"], ["turn.failed"])
        self.assertFalse(used["complete"])
        self.assertEqual(used["availability"], "partial")
        self.assertEqual(report["status"], "worker_failed")


class LiveSnapshotKeepsJobMetadata(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.report_dir = self.root / "reports" / "job-1"
        self.report_dir.mkdir(parents=True)

    def job_report(self):
        return {"job_id": "job-1", "report_dir": str(self.report_dir),
                "launcher_pid": 4242, "started_at": "2026-10-24T16:00:00+00:00",
                "workstream": "core", "job_kind": "repair", "status": None}

    def test_observer_snapshot_keeps_active_and_job_identity(self):
        observer = observation.RunObserver(self.report_dir, secrets=(), attempt=1,
                                           job_id="job-1",
                                           job={"started_at": "2026-10-24T16:00:00+00:00",
                                                "workstream": "core", "job_kind": "repair"})
        written = json.loads((self.report_dir / "progress.json").read_text())
        for name, expected in (("active", True), ("workstream", "core"),
                               ("job_kind", "repair"),
                               ("job_started_at", "2026-10-24T16:00:00+00:00")):
            self.assertEqual(written[name], expected, name)
        self.assertEqual(observer.snapshot()["active"], True)

    def test_worker_finish_does_not_finish_the_job(self):
        observer = observation.RunObserver(self.report_dir, secrets=(), attempt=1,
                                           job_id="job-1", job={"workstream": "core"})
        observer.finish(result={"returncode": 0, "timed_out": False})
        written = json.loads((self.report_dir / "progress.json").read_text())
        self.assertTrue(written["active"])
        self.assertEqual(written["workstream"], "core")

    def test_job_metadata_survives_without_an_explicit_job_argument(self):
        report = self.job_report()
        lw.progress(report, "worker_processing")
        observation.RunObserver(self.report_dir, secrets=(), attempt=2, job_id="job-1")
        written = json.loads((self.report_dir / "progress.json").read_text())
        self.assertEqual(written["job_started_at"], report["started_at"])
        self.assertEqual((written["workstream"], written["job_kind"]), ("core", "repair"))
        lw.progress(report, "verification")
        rewritten = json.loads((self.report_dir / "progress.json").read_text())
        self.assertEqual(rewritten["started_at"], report["started_at"])

    def test_quiet_snapshot_still_detects_a_dead_launcher(self):
        observer = observation.RunObserver(self.report_dir, secrets=(), attempt=1,
                                           job_id="job-1",
                                           job={"started_at": "2026-10-24T16:00:00+00:00",
                                                "workstream": "core", "job_kind": "repair"})
        item = observer.snapshot()
        with mock.patch.object(lw.os, "kill", side_effect=ProcessLookupError):
            result = lw.status_snapshot(item)
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["active"])
        self.assertEqual(result["liveness_source"], "launcher_pid")

    def test_launcher_final_progress_is_the_only_inactive_mark(self):
        report = self.job_report()
        lw.progress(report, "worker_processing")
        observation.RunObserver(self.report_dir, secrets=(), attempt=1, job_id="job-1",
                                job={"workstream": "core", "job_kind": "repair"})
        self.assertTrue(json.loads((self.report_dir / "progress.json").read_text())["active"])
        report["status"] = "ok"
        lw.progress(report, "ok")
        final = json.loads((self.report_dir / "progress.json").read_text())
        self.assertFalse(final["active"])

    def test_elapsed_seconds_follow_the_job_start_not_the_attempt_start(self):
        now = datetime.now(timezone.utc)
        item = {"active": True, "launcher_pid": os.getpid(),
                "started_at": (now - timedelta(seconds=10)).isoformat(),
                "job_started_at": (now - timedelta(seconds=40)).isoformat()}
        result = lw.status_snapshot(item)
        self.assertTrue(result["active"])
        self.assertEqual(result["liveness_source"], "launcher_pid")
        self.assertGreater(result["elapsed_seconds"], 30, result["elapsed_seconds"])
        self.assertLess(result["elapsed_seconds"], 45, result["elapsed_seconds"])


REAL_SELECTOR = selectors.DefaultSelector


class InterruptedRunCleansItsChild(unittest.TestCase):
    """An interrupt inside the shared drain loop still has to clean the group it started."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.script = self.root / "term-ignoring-pair.py"
        # A real leader that traps SIGTERM and a real descendant that inherits both pipes: an
        # interrupt that only abandoned the drain loop would leave the pair running with the
        # output still open, and neither of them dies on the signal the group is sent first.
        self.script.write_text(
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "if os.fork() == 0:\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "time.sleep(60)\n")

    def test_base_exception_terminates_the_group_and_reraises(self):
        seen = []
        real_kill_group = lw._terminate_group

        def kill_group(process):
            seen.append(process.pid)
            return real_kill_group(process)

        class InterruptingSelector:
            """The real selector, interrupted once the loop has registered both output pipes."""

            def __init__(self):
                self.inner = REAL_SELECTOR()
                self.registered = 0

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def register(self, *args, **kwargs):
                self.registered += 1
                return self.inner.register(*args, **kwargs)

            def select(self, timeout=None):
                if self.registered >= 2:
                    raise KeyboardInterrupt
                return self.inner.select(timeout)

        with mock.patch.object(observation.selectors, "DefaultSelector", InterruptingSelector), \
                mock.patch.object(lw, "_terminate_group", side_effect=kill_group) as killed:
            with self.assertRaises(KeyboardInterrupt):
                lw.run_bounded([sys.executable, "-u", str(self.script)], cwd=None,
                               env=dict(os.environ), timeout=60, stdin_data=b"")

        self.assertTrue(killed.called, "the runner must clean up with the wrapper's group killer")
        pid = seen[0]
        until = time.monotonic() + 10
        while time.monotonic() < until and observation._group_exists(pid):
            time.sleep(0.02)
        self.assertFalse(observation._group_exists(pid),
                         "the SIGTERM-ignoring leader and its descendant must both be gone")


CUTOFF_LINE = re.compile(r"- This attempt: .*?killed at (\d+\.\d)s")


class PromptStatesTheEffectiveCutoff(unittest.TestCase):
    def fixed(self, left):
        class Fixed(lw.Deadline):
            def remaining(self):
                return left

        return Fixed(600)

    def task_and_worktree(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        return lw.Task.from_payload(sp.task_payload(repo)), Path(root, "worktree")

    def test_effective_cutoff_replaces_the_bare_total(self):
        task, worktree = self.task_and_worktree()
        prompt = lw.build_prompt(task, worktree, deadline=self.fixed(300.0),
                                 attempt_timeout=180.0, reserve=60.0, attempt_cap=1200.0)
        self.assertEqual(CUTOFF_LINE.search(prompt).group(1), "180.0")
        self.assertIn("min(remaining 300.0s - verification reserve 60.0s, "
                      "per-attempt cap 1200.0s)", prompt)

    def test_cutoff_without_a_live_deadline_still_renders(self):
        task, worktree = self.task_and_worktree()
        prompt = lw.build_prompt(task, worktree, attempt_timeout=42.5, attempt_cap=300.0)
        self.assertEqual(CUTOFF_LINE.search(prompt).group(1), "42.5")

    def test_no_cutoff_claim_without_an_attempt_budget(self):
        task, worktree = self.task_and_worktree()
        self.assertNotIn("- This attempt:", lw.build_prompt(task, worktree,
                                                            deadline=self.fixed(300.0)))

    def test_the_prompt_the_worker_receives_carries_its_own_cutoff(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root)
        fake = sp.FakeRun(worker_edit=lambda cwd, attempt=1: Path(cwd, "add.py").write_text(FIX))
        with mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(sp.job_options(root / "job", repo))
        attempt_record = report["attempts"][0]
        prompt = Path(attempt_record["logs"][0]).read_text()
        self.assertEqual(CUTOFF_LINE.search(prompt).group(1),
                         f"{float(attempt_record['worker_timeout_seconds']):.1f}")
        self.assertEqual(fake.calls_of("exec")[0]["stdin"], prompt)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))


if __name__ == "__main__":
    unittest.main()
