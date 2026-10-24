"""Bounded output capture: a flooding child cannot grow launcher memory or fake success.

``run_bounded`` *is* ``observation.run_streaming``, so one runner is pinned twice against a
small patched ``CAPTURE_LIMIT_BYTES``: called directly and reached through ``run_verification``.
It must retain at most the cap per stream, kill the whole process group once the cap is crossed,
drain only for the cleanup window, and report ``output_limit_exceeded`` so neither the worker
attempt nor a verification command can be read as a pass from output nobody saw. The same runner
owns the deadline, so a descendant that outlives its exited leader while holding the inherited
pipes keeps the run on the deadline instead of buying an early exit-0 success.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import support as sp  # noqa: E402
import worker_observability as obs  # noqa: E402

lw = sp.lw
PY = sys.executable
CAP = 4096
FLOOD = 400000


class ProbeObserver:
    """Records what the streaming runner retained and fed onward, plus the child's pid."""

    def __init__(self):
        self.pids = []
        self.fed = {"stdout": 0, "stderr": 0}

    def process_started(self, pid):
        self.pids.append(pid)

    def __call__(self, name, chunk):
        self.fed[name] += len(chunk)


class StreamingCaptureLimit(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        # The leader floods stdout; a forked descendant inherits the write end of the pipe and
        # sleeps, so the pipes stay open even after the leader dies: only the bounded cleanup
        # window may keep the drain alive, and only the group kill removes the descendant.
        self.flood = self.root / "flood.py"
        self.flood.write_text(
            "import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "while True:\n"
            "    sys.stdout.write('x' * 65536)\n"
            "    sys.stdout.flush()\n")

    def test_streaming_flood_is_capped_killed_and_bounded(self):
        observer = ProbeObserver()
        started = time.monotonic()
        with mock.patch.object(obs, "CAPTURE_LIMIT_BYTES", CAP):
            result = obs.run_streaming([PY, "-u", str(self.flood)], cwd=None,
                                       env=dict(os.environ), timeout=60, observer=observer)
        elapsed = time.monotonic() - started

        self.assertTrue(result["output_limit_exceeded"], "a flood must be reported")
        self.assertFalse(result["timed_out"], "the output cap, not the deadline, ended the run")
        self.assertEqual(len(result["stdout"]), CAP, "retention is capped per stream")
        self.assertEqual(len(result["stderr"]), 0)
        self.assertLessEqual(elapsed, 10.0, f"bounded drain took {elapsed:.1f}s")
        self.assertEqual(observer.fed["stdout"], CAP, "the observer sees only retained bytes")
        self.assertEqual(observer.fed["stderr"], 0)
        self.assertNotEqual(result["returncode"], 0, "an over-cap worker cannot exit clean")
        self.assertTrue(result["termination"]["terminated"])

        pid = observer.pids[0]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and obs._group_exists(pid):
            time.sleep(.02)
        self.assertFalse(obs._group_exists(pid), "the whole process group must be cleaned up")


class RunBoundedIsTheStreamingRunner(unittest.TestCase):
    """A direct ``run_bounded`` call gets the worker runner: same cap, same kill, same drain."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.env = dict(os.environ)

    def run_child(self, code, **kwargs):
        return lw.run_bounded([PY, "-c", code], cwd=self.root, env=self.env, timeout=60, **kwargs)

    def test_large_output_is_flagged_and_bounded(self):
        code = (f"import sys\n"
                f"sys.stdout.write('y' * {FLOOD})\n"
                f"sys.stderr.write('z' * {FLOOD})\n")
        with mock.patch.object(obs, "CAPTURE_LIMIT_BYTES", CAP):
            result = self.run_child(code)

        self.assertTrue(result["output_limit_exceeded"])
        self.assertLessEqual(len(result["stdout"]), CAP)
        self.assertLessEqual(len(result["stderr"]), CAP, "no stream may outlive the cap")
        self.assertEqual(len(result["stdout"]), CAP, "the over-cap stream keeps its prefix")
        self.assertEqual(result["stdout"], b"y" * CAP, "retention keeps the leading prefix")
        self.assertFalse(result["timed_out"], "the cap, not the deadline, ended the run")
        # Crossing the cap kills the group, so whatever the command would have exited with is
        # gone: only the flag and the bounded retention may be trusted here, never a status.
        self.assertTrue(result["termination"]["terminated"])

        # Under the cap nothing is flagged, output is exact, and stdin still reaches the child.
        small = self.run_child("import sys; sys.stdout.write('a' * 100)")
        self.assertFalse(small["output_limit_exceeded"])
        self.assertEqual(small["stdout"], b"a" * 100)
        self.assertEqual(small["returncode"], 0)
        fed = lw.run_bounded(["/bin/cat"], cwd=self.root, env=self.env, timeout=60,
                             stdin_data=b"prompt through the shared runner")
        self.assertFalse(fed["output_limit_exceeded"])
        self.assertEqual(fed["stdout"], b"prompt through the shared runner")


class DescendantHoldingThePipes(unittest.TestCase):
    """An exited leader must not buy a free pass for the descendant that kept its pipes open."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.env = dict(os.environ)
        self.orphan = self.root / "orphan-writes-stdout.py"
        # The leader exits 0 at once while the forked descendant keeps both pipe write ends open
        # and sleeps, writing nothing. A runner that watched only the leader would return an
        # exit-0 success and leave the orphan behind; the deadline is what must end this run.
        self.orphan.write_text(
            "import os, time\n"
            "open(os.path.join(os.environ['ORPHAN_DIR'], 'leader.pid'), 'w')"
            ".write(str(os.getpid()))\n"
            "if os.fork() == 0:\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "os._exit(0)\n")
        self.child_env = dict(self.env, ORPHAN_DIR=str(self.root))

    def group_survivors(self):
        pid = int((self.root / "leader.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and obs._group_exists(pid):
            time.sleep(0.02)
        return obs._group_exists(pid)

    def test_the_deadline_is_held_and_the_group_is_cleaned(self):
        started = time.monotonic()
        result = lw.run_bounded([PY, "-u", str(self.orphan)], cwd=self.root,
                                env=self.child_env, timeout=2)
        elapsed = time.monotonic() - started

        self.assertTrue(result["timed_out"], "the inherited pipes, not the leader, held the run")
        self.assertGreaterEqual(elapsed, 2.0, "an early leader exit may not cut the deadline")
        self.assertLess(elapsed, 15.0, f"cleanup took {elapsed:.1f}s")
        self.assertEqual(result["returncode"], 0, "the real exit status is preserved")
        self.assertFalse(result["output_limit_exceeded"], "nothing was written, so nothing overflowed")
        self.assertTrue(result["termination"]["terminated"])
        self.assertFalse(self.group_survivors(), "the orphaned pipe holder must be gone")

    def test_zero_exit_with_inherited_pipes_cannot_be_reported_as_a_pass(self):
        sandbox = {"backend": lw.FULL_HOST_BACKEND, "available": True,
                   "network": "enabled", "profile": lw.FULL_HOST_PROFILE}
        task = lw.Task(
            task_id="orphan-bounds", repo_path=self.root, base_ref="main",
            goal="hold the deadline for an inherited pipe",
            acceptance_criteria=["no early pass"], allowed_paths=["add.py"],
            test_commands=[[PY, "-u", str(self.orphan)]], timeout_seconds=4, max_attempts=1)
        results = lw.run_verification(
            task, self.root, {"HOME": str(self.root), "PATH": os.environ.get("PATH", "/usr/bin"),
                              "ORPHAN_DIR": str(self.root)},
            "codex", sandbox, [], lw.Deadline(120))

        self.assertEqual(len(results), 1)
        entry = results[0]
        self.assertEqual(entry["returncode"], 0, "the leader really did exit 0")
        self.assertTrue(entry["timed_out"], "the descendant still held the output open")
        self.assertEqual(entry["status"], "failed", "an exit 0 with live output cannot pass")
        self.assertIn("timed out", entry["reason"])
        self.assertFalse(self.group_survivors(), "verification leaves no descendant behind")


class VerificationCannotPassOnTruncatedOutput(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.sandbox = {"backend": lw.FULL_HOST_BACKEND, "available": True,
                        "network": "enabled", "profile": lw.FULL_HOST_PROFILE}
        self.env = {"HOME": str(self.root), "PATH": os.environ.get("PATH", "/usr/bin")}

    def test_zero_exit_over_the_cap_cannot_pass(self):
        task = lw.Task(
            task_id="output-bounds", repo_path=self.root, base_ref="main",
            goal="bound captured output", acceptance_criteria=["bounded capture"],
            allowed_paths=["add.py"],
            test_commands=[[PY, "-c", f"import sys; sys.stdout.write('k' * {FLOOD})"]],
            timeout_seconds=120, max_attempts=1)
        with mock.patch.object(obs, "CAPTURE_LIMIT_BYTES", CAP):
            results = lw.run_verification(task, self.root, dict(self.env), "codex",
                                          self.sandbox, [], lw.Deadline(120))

        self.assertEqual(len(results), 1)
        entry = results[0]
        # Crossing the cap stops the group, so no exit status of this command can be trusted:
        # what is pinned is the flag, the bounded retention and the failed verdict.
        self.assertTrue(entry["output_limit_exceeded"], "the entry records the truncation")
        self.assertLessEqual(len(entry["output_text"]), 2 * CAP + 1, "retention stays capped")
        self.assertEqual(entry["status"], "failed")
        self.assertIn("capture limit", entry["reason"])
        self.assertNotIn("process group was killed", entry["reason"])
        self.assertTrue(entry["termination"]["terminated"], "termination evidence stays separate")
        self.assertEqual(len(entry["stdout_tail"]), 1000)
        reason = lw.describe_failure({"status": "tests_failed", "tests": [entry]})
        self.assertIn("cannot count as passing", reason)

        # A verification command that stays under the cap still passes normally.
        task_ok = lw.Task(task_id="output-bounds-ok", repo_path=self.root, base_ref="main",
                          goal="bound captured output", acceptance_criteria=["bounded capture"],
                          allowed_paths=["add.py"],
                          test_commands=[[PY, "-c", "print('WITHIN-CAP')"]],
                          timeout_seconds=120, max_attempts=1)
        passed = lw.run_verification(task_ok, self.root, dict(self.env), "codex",
                                     self.sandbox, [], lw.Deadline(120))
        self.assertEqual(passed[0]["status"], "passed")
        self.assertFalse(passed[0]["output_limit_exceeded"])


if __name__ == "__main__":
    unittest.main()
