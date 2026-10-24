"""Admission correctness under real concurrency: atomic ownership, real budgets, case aliases.

These are the cases a single-process metadata check cannot prove. Contention comes from
separate local Python processes and real ``fcntl.flock`` locks: a second claimer that arrives
while the first is between the slot flock and its record, a metadata update forced after the
job deadline expired, a live slot whose ownership cannot be read, and an owned path that only
differs in case. Nothing here starts a model or sleeps a long fixed time; every wait is
bounded and the parents always retire their children.
"""
import contextlib
import dataclasses
import fcntl
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

import worker_pool as wp  # noqa: E402

lw = sp.lw
SRC = str(Path(sp.lw.__file__).resolve().parent)

CHILD = '''"""One pool participant in its own process: hold a lease, probe one, or exhaust a budget."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import worker_pool as wp

args = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
state = Path(args["state"])
outcome = Path(args["outcome"])
release = Path(args["release"])
hold = float(args.get("hold", 20.0))
job_deadline = (time.monotonic() + float(args["job_seconds"])) if args.get("job_seconds") else None


def report(payload, target=outcome):
    target.write_text(json.dumps(payload), encoding="utf-8")


def wait_for(path, stop_after):
    stop = time.monotonic() + stop_after
    while not path.exists() and time.monotonic() < stop:
        time.sleep(0.02)


if args.get("gate_ready"):
    gate_ready = Path(args["gate_ready"])
    gate_release = Path(args["gate_release"])

    class GatedLease(wp.Lease):
        """Delay the owner's first record while its slot claim is already held."""

        def __init__(self, *inner, **kwargs):
            gate_ready.write_text("claiming\\n", encoding="utf-8")
            wait_for(gate_release, hold)
            super().__init__(*inner, **kwargs)

    wp.Lease = GatedLease

kwargs = dict(capacity=int(args.get("capacity", 2)), task_id=args["task_id"],
              repo_identity=wp.canonical_repo_path(args["repo"]),
              allowed_paths=list(args["allowed_paths"]),
              lock_timeout=float(args.get("lock_timeout", 5.0)),
              job_deadline=job_deadline)

try:
    if args.get("mode") == "probe":
        try:
            with wp.lease(state, **kwargs) as held:
                report({"admitted": True, "slot": held.slot})
        except wp.PoolError as refused:
            report({"admitted": False, "error": type(refused).__name__,
                    "message": str(refused)})
            raise SystemExit(1)
        raise SystemExit(0)

    with wp.lease(state, **kwargs) as held:
        report({"admitted": True, "slot": held.slot, "token": held.token,
                "metadata_deadline": held.metadata_deadline})
        if args.get("mode") == "metadata":
            steps = {"admitted": True}
            try:
                wait_for(Path(args["proceed"]), hold)
                started = time.monotonic()
                try:
                    held.update(worktree="/tmp/late-worktree")
                    steps["update"] = "allowed"
                except wp.PoolError as refused:
                    steps["update"] = type(refused).__name__
                steps["update_seconds"] = round(time.monotonic() - started, 3)
                started = time.monotonic()
                steps["forget"] = held.forget()
                steps["forget_seconds"] = round(time.monotonic() - started, 3)
            except Exception as late_error:  # noqa: BLE001 - keep the report on a crash
                steps["error"] = f"{type(late_error).__name__}: {late_error}"
            report(steps, outcome.parent / "steps.json")
        wait_for(release, hold)
except Exception as error:  # noqa: BLE001 - reported through the outcome file
    report({"error": f"{type(error).__name__}: {error}"})
    raise SystemExit(1)
'''


class PoolAtomicityCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.state = self.root / "state"
        self.script = self.root / "child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.repo = sp.make_repo(self.root / "repo-a")
        self.other = sp.make_repo(self.root / "repo-b")
        self.third = sp.make_repo(self.root / "repo-c")

    # -- child processes ----------------------------------------------------------
    def spawn(self, name, **args):
        directory = self.root / "children" / name
        directory.mkdir(parents=True)
        args.setdefault("state", str(self.state))
        args.setdefault("hold", 20.0)
        args["outcome"] = str(directory / "outcome.json")
        args["release"] = str(directory / "release")
        args.setdefault("proceed", str(directory / "proceed"))
        (directory / "args.json").write_text(json.dumps(args), encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(self.script), SRC,
                                    str(directory / "args.json")],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.retire, process, Path(args["release"]))
        return {"process": process, "directory": directory, "name": name,
                "outcome": Path(args["outcome"]), "release": Path(args["release"])}

    @staticmethod
    def tail(child):
        process = child["process"]
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        return f"rc={process.returncode} out={stdout[-300:]!r} err={stderr[-300:]!r}"

    def outcome(self, child, timeout=20.0):
        deadline = time.monotonic() + timeout
        while not child["outcome"].exists():
            if time.monotonic() > deadline:
                self.fail(f"{child['name']} never reported: {self.tail(child)}")
            time.sleep(0.02)
        return json.loads(child["outcome"].read_text(encoding="utf-8"))

    def outcome_step(self, child, name="steps", timeout=20.0):
        """Read a secondary report file that the child writes later in its life."""
        path = child["directory"] / f"{name}.json"
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"{child['name']} never wrote {name}: {self.tail(child)}")
            time.sleep(0.02)
        return json.loads(path.read_text(encoding="utf-8"))

    def wait_for_path(self, path, child, timeout=20.0):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"{child['name']} never reached {path.name}: {self.tail(child)}")
            time.sleep(0.02)
        return path

    @staticmethod
    def retire(process, release):
        release.write_text("release\n", encoding="utf-8")
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def wait_for_no_active(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while wp.active_leases(self.state) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(wp.active_leases(self.state), [])

    # -- in-process helpers -------------------------------------------------------
    def lease(self, repo=None, allowed_paths=("add.py",), capacity=2, timeout=0.4,
              task_id="probe", job_deadline=None):
        return wp.lease(self.state, capacity=capacity, task_id=task_id,
                        repo_identity=lw.repo_identity(repo if repo is not None else self.repo),
                        allowed_paths=list(allowed_paths), lock_timeout=timeout,
                        job_deadline=job_deadline)

    @contextlib.contextmanager
    def hold_coordinator(self, timeout=10.0):
        """Take the coordinator from the test process, the way a busy claimer would."""
        handle = open(self.state / "coordinator.lock", "a+", encoding="utf-8")
        self.addCleanup(handle.close)
        deadline = time.monotonic() + timeout
        while not self.flock_nowait(handle):
            if time.monotonic() >= deadline:
                self.fail("the coordinator was never released")
            time.sleep(0.02)
        try:
            yield handle
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def flock_nowait(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def coordinator_is_held(self):
        handle = open(self.state / "coordinator.lock", "a+", encoding="utf-8")
        try:
            return not self.flock_nowait(handle)
        finally:
            handle.close()

    def published_records(self):
        return {path.name: wp._read_json(path)
                for path in sorted((self.state / "leases").glob("*.json"))}


class AtomicPublication(PoolAtomicityCase):
    def test_claim_and_publication_are_one_critical_section(self):
        """A competitor must never see a live slot that has no owner yet."""
        gate_ready = self.root / "gate-ready"
        gate_release = self.root / "gate-release"
        holder = self.spawn("owner", mode="hold", task_id="owner", repo=str(self.repo),
                            allowed_paths=["Sources"], capacity=2, gate_ready=str(gate_ready),
                            gate_release=str(gate_release))

        self.wait_for_path(gate_ready, holder)          # slot claimed, inside the critical pass
        self.assertTrue(wp.slot_is_live(self.state, 1), "the slot must already be claimed")
        self.assertTrue(self.coordinator_is_held(), "the claim pass must still own the pool")
        self.assertEqual(self.published_records(), {}, "no owner may be published yet")
        self.assertEqual([record.get("record_unreadable")
                          for record in wp.active_leases(self.state)], [True],
                         "the live slot must be reported as unverifiable, not invisible")

        gap = self.spawn("during-gap", mode="probe", task_id="competitor-gap",
                         repo=str(self.repo), allowed_paths=["Sources/file.py"],
                         capacity=2, lock_timeout=0.6)
        gap_report = self.outcome(gap)
        self.assertEqual(gap["process"].wait(timeout=10), 1)
        self.assertFalse(gap_report["admitted"],
                         f"admitted through the gap: {gap_report}")
        self.assertEqual(gap_report["error"], "PoolUnavailableError")
        self.assertEqual(len(wp.active_leases(self.state)), 1, "a refused probe owns nothing")

        gate_release.write_text("publish\n", encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            records = [record for record in wp.active_leases(self.state)
                       if record.get("task_id") == "owner"]
            if records:
                break
            time.sleep(0.02)
        else:
            self.fail(f"the owner never published: {wp.active_leases(self.state)}")
        self.assertEqual(records[0]["allowed_paths"], ["Sources"])

        after = self.spawn("after-publication", mode="probe", task_id="competitor-after",
                           repo=str(self.repo), allowed_paths=["Sources/file.py"],
                           capacity=2, lock_timeout=0.6)
        after_report = self.outcome(after)
        self.assertFalse(after_report["admitted"])
        self.assertEqual(after_report["error"], "PoolConflictError")
        self.assertIn("Sources", after_report["message"])

        holder["release"].write_text("release\n", encoding="utf-8")
        holder["process"].communicate(timeout=15)
        self.wait_for_no_active()


class UnverifiedOwnership(PoolAtomicityCase):
    def test_live_slot_with_unknown_owner_blocks_admission(self):
        with self.lease(allowed_paths=["notes.md"], task_id="owner") as owner:
            record = self.state / "leases" / f"slot-{owner.slot}.json"
            self.assertTrue(record.is_file())
            record.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(wp.PoolUnavailableError) as refused:
                with self.lease(allowed_paths=["elsewhere.py"], task_id="probe"):
                    self.fail("admitted although no live owner could be identified")
            self.assertIn("unreadable", str(refused.exception))
            # A provably different repository is still not a licence to guess.
            with self.assertRaises(wp.PoolUnavailableError):
                with self.lease(repo=self.other, allowed_paths=["notes.md"], task_id="other"):
                    self.fail("admitted against an unidentified live owner")

    def test_stale_metadata_on_a_free_slot_is_ignored(self):
        with self.lease(allowed_paths=["notes.md"], task_id="owner") as owner:
            record = self.state / "leases" / f"slot-{owner.slot}.json"
            record.write_text("{ not json", encoding="utf-8")
        self.assertEqual(wp.active_leases(self.state), [])
        self.assertTrue(record.is_file(), "leftover metadata alone is not activity")
        with self.lease(allowed_paths=["notes.md"], task_id="reclaimer", timeout=1.0) as late:
            self.assertEqual(late.slot, owner.slot)
            self.assertEqual(late.allowed_paths, ["notes.md"])


class JobDeadlineBoundsMetadata(PoolAtomicityCase):
    def test_update_after_the_job_deadline_is_bounded_and_locks_are_released(self):
        holder = self.spawn("budget", mode="metadata", task_id="budgeted",
                           repo=str(self.repo), allowed_paths=["add.py"], capacity=2,
                           job_seconds=0.3, lock_timeout=5.0)
        admitted = self.outcome(holder)
        self.assertTrue(admitted["admitted"])
        slot = admitted["slot"]
        record = self.state / "leases" / f"slot-{slot}.json"
        self.assertGreater(admitted["metadata_deadline"], 0.0,
                           "the holder must carry a real metadata deadline")

        # The assertion below is a precondition of the scenario, not a race to win: the child's
        # metadata write is only meaningful once its own job budget has been spent. On a fast
        # interpreter the parent otherwise reaches the check before the 0.3s child deadline has
        # elapsed. Wait explicitly (bounded) for the observed deadline to expire instead of
        # relaxing the budget, so the cross-process locking assertions stay exactly as strict.
        expiry_bound = time.monotonic() + 5.0
        while time.monotonic() < admitted["metadata_deadline"]:
            if time.monotonic() > expiry_bound:
                self.fail("the holder's job deadline %r never expired within 5s: %s"
                          % (admitted["metadata_deadline"], self.tail(holder)))
            time.sleep(0.02)

        proceed = holder["directory"] / "proceed"
        with self.hold_coordinator():
            self.assertTrue(wp.slot_is_live(self.state, slot))
            self.assertLess(admitted["metadata_deadline"], time.monotonic(),
                            "the original job budget must already be spent before the write")
            proceed.write_text("go\n", encoding="utf-8")
            steps = self.outcome_step(holder)
            self.assertNotIn("error", steps, f"the child crashed: {steps}")
            self.assertEqual(steps["update"], "PoolUnavailableError",
                             "a metadata write must not wait 30s past the job deadline")
            self.assertLess(steps["update_seconds"], 2.0,
                            f"update waited {steps['update_seconds']}s")
            self.assertFalse(steps["forget"], "cleanup cannot reach the coordinator")
            self.assertLess(steps["forget_seconds"], 2.0)
            self.assertTrue(record.is_file(), "inactive metadata may stay, the job may not")
            self.assertNotEqual(wp._read_json(record).get("worktree"), "/tmp/late-worktree")
            self.assertTrue(wp.slot_is_live(self.state, slot), "the slot stays held for its job")

        holder["release"].write_text("release\n", encoding="utf-8")
        holder["process"].communicate(timeout=15)
        self.assertFalse(wp.slot_is_live(self.state, slot), "a finished job releases its slot")
        self.wait_for_no_active()

    def test_admission_never_outlives_the_job_deadline(self):
        holder = self.spawn("short-job", mode="probe", task_id="short", repo=str(self.repo),
                            allowed_paths=["add.py"], capacity=2, job_seconds=0.4,
                            lock_timeout=10.0)
        admitted = self.outcome(holder)
        self.assertTrue(admitted["admitted"])
        started = time.monotonic()
        with self.hold_coordinator():
            blocked = self.spawn("blocked", mode="probe", task_id="blocked",
                                 repo=str(self.other), allowed_paths=["add.py"], capacity=2,
                                 job_seconds=0.4, lock_timeout=10.0)
            refused = self.outcome(blocked)
        self.assertFalse(refused["admitted"])
        self.assertEqual(refused["error"], "PoolUnavailableError")
        self.assertLess(time.monotonic() - started, 5.0,
                        "a 10s lock wait must be clipped to the remaining job budget")

    def test_caller_deadline_end_is_carried_into_the_holder(self):
        job_deadline = time.monotonic() + 12.0
        with self.lease(allowed_paths=["add.py"], task_id="carried",
                        job_deadline=job_deadline) as held:
            self.assertEqual(held.metadata_deadline, job_deadline)
            held.update(worktree="/tmp/carried")
            self.assertEqual(wp._read_json(
                self.state / "leases" / f"slot-{held.slot}.json")["worktree"], "/tmp/carried")

    def test_job_lease_forwards_the_job_deadline_to_the_pool(self):
        options = dataclasses.replace(sp.job_options(self.root, self.repo), maximum=2)
        job_deadline = time.monotonic() + 30.0
        seen = {}
        real = wp.lease

        def spy(*argv, **kwargs):
            seen.update(kwargs)
            return real(*argv, **kwargs)

        report = {"job_id": "report-1"}
        with mock.patch.object(lw.pool, "lease", spy):
            with lw.job_lease(options, report, 1.0, job_deadline=job_deadline) as held:
                self.assertIsNotNone(held)
        self.assertEqual(seen["job_deadline"], job_deadline)
        self.assertEqual(held.metadata_deadline, job_deadline)
        self.assertEqual(report["lease"]["capacity"], 2)


class CaseAliasOwnership(PoolAtomicityCase):
    def test_case_alias_owned_path_is_refused(self):
        with self.lease(allowed_paths=["Sources/Settings.swift"], task_id="owner"):
            with self.assertRaises(wp.PoolConflictError) as refused:
                with self.lease(allowed_paths=["Sources/settings.swift"], task_id="alias"):
                    self.fail("admitted a case alias of a live owned file")
            self.assertIn("settings.swift", str(refused.exception))

    def test_case_alias_directory_and_glob_root_are_refused(self):
        with self.lease(allowed_paths=["Sources/*"], task_id="owner"):
            with self.assertRaises(wp.PoolConflictError):
                with self.lease(allowed_paths=["sources/Notes.md"], task_id="alias-dir"):
                    self.fail("admitted a case alias of a live owned directory")
        with self.lease(allowed_paths=["Sources/*"], task_id="owner-two"):
            with self.assertRaises(wp.PoolConflictError):
                with self.lease(allowed_paths=["sources/*"], task_id="alias-glob"):
                    self.fail("admitted a case alias of a live owned glob root")

    def test_genuinely_disjoint_paths_still_admit(self):
        with self.lease(allowed_paths=["Sources/Alpha.swift"], task_id="owner"):
            with self.lease(repo=self.other, allowed_paths=["tests/beta.py"],
                            task_id="other-repo"):
                pass
            with self.lease(allowed_paths=["tests/beta.py"], task_id="disjoint",
                            timeout=1.0) as late:
                self.assertIsNotNone(late.slot)
                self.assertEqual(late.allowed_paths, ["tests/beta.py"])

    def test_overlap_helper_casefolds_without_losing_the_original_specs(self):
        self.assertEqual(wp.owned_paths_overlap(["Sources/Settings.swift"],
                                                ["Sources/settings.swift"]),
                         ("Sources/Settings.swift", "Sources/settings.swift"))
        self.assertEqual(wp.owned_paths_overlap(["Documentation/*"], ["documentation/intro.md"]),
                         ("Documentation/*", "documentation/intro.md"))
        self.assertIsNone(wp.owned_paths_overlap(["Sources/Alpha.swift"], ["tests/alpha.swift"]))
        self.assertIsNone(wp.owned_paths_overlap(["Sources/a.py"], ["Tests/b.py"]))
        self.assertEqual(wp._fold("/Repo/Sources"), wp._fold("/repo/sources"))

    def test_repo_identity_is_compared_case_insensitively(self):
        records = [{"slot": 1, "task_id": "owner", "job_id": None, "capacity": 2,
                    "repo_identity": "/Repo/Sources-Like", "allowed_paths": ["Sources/a.py"]}]
        refusal = wp._admission_refusal(records, "/repo/sources-like", ["Sources/a.py"])
        self.assertIsInstance(refusal, wp.PoolConflictError)
        self.assertIsNone(wp._admission_refusal(records, "/repo/elsewhere", ["Sources/a.py"]))


class MixedCapacities(PoolAtomicityCase):
    def test_lowest_active_ceiling_is_honored(self):
        """A ceiling-2 job may not let a ceiling-4 job push the pool past two writers."""
        with self.lease(allowed_paths=["a.py"], capacity=2, task_id="two") as first:
            self.assertEqual(first.capacity, 2)
            with self.lease(repo=self.other, allowed_paths=["b.py"],
                            capacity=wp.MAX_WORKERS_LIMIT, task_id="four") as second:
                self.assertLessEqual(second.slot, 2, "a mixed pool must honor the ceiling of 2")
                self.assertEqual(sorted(record["slot"] for record in wp.active_leases(self.state)),
                                 [1, 2])
                with self.assertRaises(wp.PoolUnavailableError):
                    with self.lease(repo=self.third, allowed_paths=["c.py"],
                                    capacity=wp.MAX_WORKERS_LIMIT, task_id="third"):
                        self.fail("a third writer joined a pool capped at 2 by an active job")
                self.assertEqual(len(wp.active_leases(self.state)), 2)
        self.assertEqual(wp.active_leases(self.state), [])
        with self.lease(repo=self.third, allowed_paths=["c.py"],
                        capacity=wp.MAX_WORKERS_LIMIT, task_id="alone", timeout=1.0) as alone:
            self.assertEqual(alone.slot, 1)
            self.assertLessEqual(alone.slot, wp.MAX_WORKERS_LIMIT)

    def test_effective_capacity_refuses_a_stricter_active_ceiling(self):
        records = [{"slot": 1, "capacity": 2}, {"slot": 2, "capacity": 4}]
        self.assertEqual(wp._effective_capacity(records, wp.MAX_WORKERS_LIMIT), 2)
        self.assertEqual(wp._effective_capacity([{"slot": 1, "capacity": "two"}], 4), 1)
        self.assertEqual(wp._effective_capacity([], wp.MAX_WORKERS_LIMIT), wp.MAX_WORKERS_LIMIT)


if __name__ == "__main__":
    unittest.main()
