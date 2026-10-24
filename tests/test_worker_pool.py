"""Bounded shared-state concurrency: real processes, real file locks, no inference.

Nothing here starts a model, a server or a simulator. Contention is produced by separate
local Python processes that hold pool leases with ``fcntl.flock`` while the test asserts
what a second job can see: capacity, ownership refusals, device serialization, and that a
killed or failed holder leaks neither a slot nor a lock.
"""
import contextlib
import dataclasses
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

import worker_pool as wp  # noqa: E402

lw = sp.lw
SRC = str(Path(sp.lw.__file__).resolve().parent)
FIX = "def add(a, b):\n    return a + b\n"

HOLDER = '''"""Hold one pool lease (or the legacy worker lock) until told to let go."""
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import worker_pool as wp

args = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
state = Path(args["state"])
ready = Path(args["ready"])
release = Path(args["release"])
try:
    with contextlib.ExitStack() as stack:
        held = None
        if args.get("mode") == "legacy":
            import local_worker as worker
            stack.enter_context(worker.job_lock(state, float(args["hold"])))
        else:
            held = stack.enter_context(
                wp.lease(state, capacity=int(args["capacity"]), task_id=args["task_id"],
                         repo_identity=wp.canonical_repo_path(args["repo"]),
                         allowed_paths=list(args["allowed_paths"]),
                         job_id=args.get("job_id"), lock_timeout=float(args["hold"]),
                         simulator_udid=args.get("udid")))
        ready.write_text(json.dumps({"slot": getattr(held, "slot", None)}), encoding="utf-8")
        stop = time.monotonic() + float(args["hold"])
        while not release.exists() and time.monotonic() < stop:
            time.sleep(0.02)
except Exception as error:  # noqa: BLE001 - reported through the ready file
    ready.write_text(json.dumps({"error": f"{type(error).__name__}: {error}"}),
                     encoding="utf-8")
    raise SystemExit(1)
'''


class PoolCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.state = self.root / "state"
        self.script = self.root / "holder.py"
        self.script.write_text(HOLDER, encoding="utf-8")
        self.repo = sp.make_repo(self.root / "repo-a")
        self.other = sp.make_repo(self.root / "repo-b")

    # -- helpers ------------------------------------------------------------------
    def hold(self, name, *, repo=None, allowed_paths, capacity=wp.MAX_WORKERS_LIMIT,
             mode="pool", udid=None, hold_for=30.0):
        directory = self.root / "holders" / name
        directory.mkdir(parents=True)
        ready, release = directory / "ready.json", directory / "release"
        args = {"state": str(self.state), "ready": str(ready), "release": str(release),
                "task_id": f"job-{name}", "job_id": f"report-{name}", "mode": mode,
                "capacity": capacity, "hold": hold_for, "udid": udid,
                "repo": str(repo if repo is not None else self.repo),
                "allowed_paths": list(allowed_paths)}
        (directory / "args.json").write_text(json.dumps(args), encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(self.script), SRC,
                                    str(directory / "args.json")],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.addCleanup(self.retire, process, release)
        deadline = time.monotonic() + 20.0
        while not ready.exists():
            if time.monotonic() > deadline:
                self.fail(f"holder {name} never became ready: {self.tail(process)}")
            if process.poll() is not None:
                self.fail(f"holder {name} exited early: {self.tail(process)}")
            time.sleep(0.02)
        outcome = json.loads(ready.read_text(encoding="utf-8"))
        if "error" in outcome:
            self.fail(f"holder {name} refused: {outcome['error']}")
        return {"process": process, "release": release, "slot": outcome["slot"],
                "name": name}

    @staticmethod
    def tail(process):
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        return f"rc={process.returncode} out={stdout[-400:]!r} err={stderr[-400:]!r}"

    @staticmethod
    def retire(process, release):
        release.write_text("release\n", encoding="utf-8")
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    @staticmethod
    def free(holders):
        for holder in holders:
            holder["release"].write_text("release\n", encoding="utf-8")

    def wait_for_active(self, count, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = wp.active_leases(self.state)
            if len(active) == count:
                return active
            time.sleep(0.02)
        self.fail(f"expected {count} active lease(s), found {wp.active_leases(self.state)}")

    def lease(self, repo=None, allowed_paths=("add.py",), capacity=2, udid=None,
              timeout=0.4, task_id="probe"):
        return wp.lease(self.state, capacity=capacity, task_id=task_id,
                        repo_identity=lw.repo_identity(repo if repo is not None else self.repo),
                        allowed_paths=list(allowed_paths), lock_timeout=timeout,
                        simulator_udid=udid)


class Capacity(PoolCase):
    def test_four_simultaneous_leases_and_a_fifth_job_is_blocked(self):
        holders = [self.hold(f"p{index}", repo=(self.repo, self.other)[index % 2],
                             allowed_paths=[f"held{index}.py"])
                   for index in range(1, wp.MAX_WORKERS_LIMIT + 1)]
        self.assertEqual(sorted(holder["slot"] for holder in holders), [1, 2, 3, 4])
        active = self.wait_for_active(wp.MAX_WORKERS_LIMIT)
        self.assertEqual(sorted(item["task_id"] for item in active),
                         [f"job-p{index}" for index in range(1, 5)])
        self.assertEqual(len({Path(item["repo_identity"]) for item in active}), 2)
        started = time.monotonic()
        with self.assertRaises(wp.PoolUnavailableError) as refused:
            with self.lease(allowed_paths=("late.py",), capacity=wp.MAX_WORKERS_LIMIT,
                            timeout=0.5):
                self.fail("a fifth lease must not be granted")
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("all 4 worker slot(s)", str(refused.exception))
        self.assertIn("busy", str(refused.exception))

        self.free(holders)
        deadline = time.monotonic() + 15.0
        while wp.active_leases(self.state) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(wp.active_leases(self.state), [])
        with self.lease(allowed_paths=("late.py",), capacity=wp.MAX_WORKERS_LIMIT,
                        timeout=2.0) as late:
            self.assertEqual(late.slot, 1)
            self.assertEqual(late.mode, "parallel")
            self.assertEqual(late.as_report()["legacy_lock"], "shared")
        self.assertEqual(wp.active_leases(self.state), [])


class LegacyInterop(PoolCase):
    def test_default_one_blocks_a_parallel_job(self):
        self.hold("legacy", mode="legacy", allowed_paths=["add.py"], hold_for=20.0)
        time.sleep(0.2)
        with self.assertRaises(wp.PoolUnavailableError):
            with self.lease(capacity=wp.MAX_WORKERS_LIMIT, timeout=0.4):
                self.fail("parallel work must not start while a default job holds the lock")
        self.assertEqual(wp.active_leases(self.state), [])

    def test_parallel_job_blocks_a_default_one(self):
        holder = self.hold("parallel", allowed_paths=["add.py"])
        self.wait_for_active(1)
        started = time.monotonic()
        with self.assertRaises(lw.BlockedError) as refused:
            with lw.job_lock(self.state, timeout=0.4):
                self.fail("the default single-worker lock must stay exclusive")
        self.assertIn("another local worker", str(refused.exception))
        self.assertLess(time.monotonic() - started, 5.0)
        self.free([holder])
        deadline = time.monotonic() + 15.0
        while wp.active_leases(self.state) and time.monotonic() < deadline:
            time.sleep(0.02)
        with lw.job_lock(self.state, timeout=2.0):
            self.assertIn(str(os.getpid()), (self.state / "worker.lock").read_text())


class Ownership(PoolCase):
    def assert_refused(self, first, second, repo=None, other_repo=None):
        with self.lease(repo=repo, allowed_paths=list(first), task_id="owner-one"):
            with self.assertRaises(wp.PoolConflictError) as refused:
                with self.lease(repo=repo, allowed_paths=list(second), task_id="owner-two"):
                    self.fail(f"{second} overlaps {first} and must be refused")
        return str(refused.exception)

    def test_overlapping_owned_paths_are_refused_with_the_other_job_named(self):
        for first, second in (("add.py", "add.py"), ("src", "src/app.py"),
                              ("src/*", "src/app.py"), ("*.md", "notes.md")):
            with self.subTest(first=first, second=second):
                reason = self.assert_refused((first,), (second,))
                self.assertIn("owner-one", reason)
                self.assertIn("slot 1", reason)
                self.assertIn("separate worktrees", reason.lower())
                self.assertIn("sequentially", reason)

    def test_disjoint_owned_paths_and_other_repositories_are_allowed(self):
        with self.lease(allowed_paths=("src/app.py",), capacity=4,
                        task_id="owner-one") as first:
            self.assertEqual(first.slot, 1)
            self.wait_for_active(1)
            with self.lease(allowed_paths=("notes.md",), capacity=4,
                            task_id="owner-two") as second:
                self.assertEqual(second.slot, 2)
                self.wait_for_active(2)
                with self.lease(repo=self.other, allowed_paths=("src/app.py",),
                                capacity=4, task_id="owner-three") as third:
                    self.assertEqual(third.slot, 3)
                    self.assertEqual(len(wp.active_leases(self.state)), 3)
        self.assertEqual(wp.active_leases(self.state), [])

    def test_lease_metadata_is_recorded_and_owner_only(self):
        with self.lease(allowed_paths=("add.py",), task_id="meta") as held:
            held.update(worktree="/tmp/wt-meta", report_dir="/tmp/report-meta")
            record = self.wait_for_active(1)[0]
            self.assertEqual(record["worktree"], "/tmp/wt-meta")
            self.assertEqual(record["report_dir"], "/tmp/report-meta")
            self.assertEqual(record["allowed_paths"], ["add.py"])
            self.assertEqual(record["task_id"], "meta")
            with self.assertRaises(wp.PoolConfigError):
                held.update(allowed_paths=["everywhere"])
        self.assertEqual(wp.active_leases(self.state), [])


class DeviceLeases(PoolCase):
    def test_same_simulator_uuid_is_serialized_case_insensitively(self):
        with self.lease(allowed_paths=("add.py",), udid=sp.APPLE_UDID, task_id="native-one"):
            with self.assertRaises(wp.PoolUnavailableError) as refused:
                with self.lease(allowed_paths=("notes.md",), udid=sp.APPLE_UDID.lower(),
                                task_id="native-two", timeout=0.4):
                    self.fail("one simulator must never be installed twice at once")
            self.assertIn(sp.APPLE_UDID.lower(), str(refused.exception))
            with self.lease(allowed_paths=("notes.md",), udid=sp.OTHER_UDID,
                            task_id="native-three") as other:
                self.assertEqual(other.resource,
                                 f"apple-simulator:{sp.OTHER_UDID.lower()}")
        self.assertEqual(wp.active_leases(self.state), [])

    def test_invalid_device_identifier_is_refused(self):
        with self.assertRaises(wp.PoolConfigError):
            with self.lease(udid="../../etc/passwd"):
                pass


class ReleaseBehaviour(PoolCase):
    def test_killed_holder_releases_the_kernel_locks(self):
        holder = self.hold("victim", allowed_paths=["add.py"])
        self.wait_for_active(1)
        slot = holder["slot"]
        stale = self.state / "leases" / f"slot-{slot}.json"
        self.assertTrue(stale.is_file())
        os.kill(holder["process"].pid, signal.SIGKILL)
        holder["process"].communicate(timeout=15)
        self.assertTrue(stale.is_file())               # metadata alone is not activity
        self.assertEqual(wp.active_leases(self.state), [])
        with self.lease(allowed_paths=("add.py",), timeout=1.0) as reused:
            self.assertEqual(reused.slot, slot)

    def test_exception_and_timeout_never_leak_a_slot(self):
        with self.assertRaises(RuntimeError):
            with self.lease(allowed_paths=("add.py",), task_id="boom"):
                raise RuntimeError("worker body failed")
        self.assertEqual(wp.active_leases(self.state), [])

        holders = [self.hold(f"busy{index}", allowed_paths=[f"f{index}.py"])
                   for index in (1, 2)]
        self.wait_for_active(2)
        for _ in range(2):
            with self.assertRaises(wp.PoolUnavailableError):
                with self.lease(allowed_paths=("late.py",), capacity=2, timeout=0.2):
                    pass
        self.assertEqual(len(wp.active_leases(self.state)), 2)
        self.free(holders)
        deadline = time.monotonic() + 15.0
        while wp.active_leases(self.state) and time.monotonic() < deadline:
            time.sleep(0.02)
        with self.lease(allowed_paths=("late.py",), capacity=2, timeout=1.0):
            self.assertEqual(len(wp.active_leases(self.state)), 1)


class Ceiling(unittest.TestCase):
    def test_only_integers_one_to_four_are_accepted(self):
        for value in (1, 2, 3, 4):
            self.assertEqual(wp.validate_max_workers(value), value)
        for value in (True, False, 0, 5, -1, "2", 2.0, None, "four"):
            with self.subTest(value=value):
                with self.assertRaises(wp.PoolConfigError) as refused:
                    wp.validate_max_workers(value)
                self.assertIn("max_workers", str(refused.exception))

    def test_repo_identity_groups_worktrees_of_one_repository(self):
        case = sp.tmpdir()
        self.addCleanup(sp.cleanup, case)
        repo = sp.make_repo(case / "repo")
        commit = lw.rev_parse(repo, "HEAD")
        linked = case / "wt"
        lw.add_worktree(repo, commit, linked)
        self.assertEqual(lw.repo_identity(linked), lw.repo_identity(repo))
        self.assertNotEqual(lw.repo_identity(repo), lw.repo_identity(sp.make_repo(case / "b")))


class JobWiring(PoolCase):
    def options(self, **overrides):
        return dataclasses.replace(sp.job_options(self.root, self.repo), **overrides)

    def run_job(self, options):
        with mock.patch.object(lw, "run_bounded", sp.FakeRun(worker_edit=self.fix)):
            return lw.run_job(options)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX)

    def test_parallel_job_reports_its_slot_and_releases_it(self):
        options = self.options(maximum=3)
        report = self.run_job(options)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        lease = report["lease"]
        self.assertEqual((lease["mode"], lease["capacity"], lease["legacy_lock"]),
                         ("parallel", 3, "shared"))
        self.assertEqual(lease["slot"], 1)
        self.assertEqual(lease["repo_identity"], lw.repo_identity(self.repo))
        self.assertIsInstance(lease["wait_seconds"], float)
        self.assertTrue(Path(report["worktree"]).is_dir())
        self.assertEqual(wp.active_leases(options.state_dir), [])
        self.assertIn("lease: slot 1/3", "\n".join(lw.summary_lines(report)))

    def test_default_capacity_keeps_the_exclusive_legacy_lock(self):
        report = self.run_job(self.options())
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(report["lease"]["mode"], "exclusive")
        self.assertIsNone(report["lease"]["slot"])
        self.assertEqual(report["lease"]["legacy_lock"], "exclusive")
        self.assertIn(str(os.getpid()), (self.state / "worker.lock").read_text())

    def test_parallel_job_is_blocked_by_an_active_overlap(self):
        options = dataclasses.replace(self.options(maximum=4),
                                      task=dataclasses.replace(
                                          self.options().task, allowed_paths=["add.py"]))
        with self.lease(repo=self.repo, allowed_paths=("add.py",), capacity=4,
                        task_id="owner-one"):
            report = self.run_job(options)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("owner-one", report["failure_reason"])
        self.assertIn("separate worktrees", report["failure_reason"].lower())
        self.assertIsNone(report["worktree"])
        self.assertFalse((options.state_dir / "worktrees").exists())

    def test_native_opt_in_claims_device_even_when_bypass_is_refused(self):
        # verify=False keeps this test free of simulator and native calls; the resource
        # lease is taken before anything runs, so it covers probe and verification alike.
        options = dataclasses.replace(
            self.options(maximum=2, verify=False, apple_simulator_udid=sp.OTHER_UDID),
            task=dataclasses.replace(self.options().task,
                                     allowed_paths=["notes.md", "add.py"]))
        with mock.patch.object(lw, "run_bounded", sp.FakeRun(worker_edit=self.fix)):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("native", report["failure_reason"])
        self.assertEqual(report["lease"]["resource"],
                         f"apple-simulator:{sp.OTHER_UDID.lower()}")
        self.assertTrue((options.state_dir / "resources" /
                         f"apple-simulator-{sp.OTHER_UDID.lower()}.lock").is_file())
        self.assertEqual(wp.active_leases(options.state_dir), [])

    def test_invalid_capacity_is_refused_before_any_worktree_exists(self):
        for value in (0, 5, True, "2", None):
            with self.subTest(value=value):
                options = self.options(maximum=value)
                report = self.run_job(options)
                self.assertEqual(report["status"], "contract_invalid")
                self.assertIn("max_workers", report["failure_reason"])
                self.assertIsNone(report["worktree"])
                self.assertFalse((options.state_dir / "worktrees").exists())
                self.assertEqual(wp.active_leases(options.state_dir), [])

    def test_command_line_ceiling_is_validated_before_launching(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        repo = sp.make_repo(root / "repo")
        task_file = root / "task.json"
        task_file.write_text(json.dumps(sp.task_payload(repo)), encoding="utf-8")
        argv = ["run", "--task-file", str(task_file), "--state-dir", str(root / "state"),
                "--codex-bin", "codex-mock"]
        for value, expected in (("5", 1), ("0", 1), ("banana", 1)):
            with self.subTest(value=value):
                printed = io.StringIO()
                with redirect_stdout(printed):
                    self.assertEqual(lw.main(argv + ["--max-workers", value]), expected)
                self.assertIn("max_workers", printed.getvalue())
                self.assertFalse((root / "state" / "worktrees").exists())
        self.assertEqual(lw.build_parser().parse_args(
            argv + ["--max-workers", "4"]).max_workers, "4")
        self.assertEqual(lw.build_parser().parse_args(argv).max_workers, "1")


if __name__ == "__main__":
    unittest.main()
