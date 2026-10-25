"""Signal handling and safe cleanup of worker groups left by crashed launchers."""
import json
import contextlib
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_worker as lw  # noqa: E402
import process_supervisor  # noqa: E402


def kill_members_with_matching_birth_tokens(members):
    """Test cleanup by captured PID/birth/SID, never by a possibly reused PGID."""
    for member in members:
        pid, pgid, sid = member["pid"], member["pgid"], member["sid"]
        identity = process_supervisor.process_start_identity(pid)
        if not identity or identity != member.get("birth_identity"):
            continue
        try:
            if os.getsid(pid) == sid and sid == pgid:
                os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def owned_group_snapshot(pgid):
    return [dict(item, birth_identity=process_supervisor.process_start_identity(item["pid"]))
            for item in (process_supervisor.process_group_members(pgid) or [])]


class OrphanReaping(unittest.TestCase):
    def test_keeper_installs_term_and_release_handlers_before_spawning_command(self):
        old_term = signal.getsignal(signal.SIGTERM)
        old_release = signal.getsignal(signal.SIGUSR1)
        observed = {}

        class Command:
            def wait(self):
                return 0

        def spawn(_argv):
            observed["term"] = signal.getsignal(signal.SIGTERM)
            observed["release"] = signal.getsignal(signal.SIGUSR1)
            return Command()

        try:
            with mock.patch.object(process_supervisor.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(process_supervisor, "_group_has_other_members",
                                      return_value=False):
                self.assertEqual(process_supervisor.main(["/bin/true"]), 0)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGUSR1, old_release)
        self.assertTrue(callable(observed["term"]))
        self.assertTrue(callable(observed["release"]))

    def test_keeper_release_wait_uses_bounded_polling_without_a_pause_gap(self):
        checks = {"count": 0}

        def released():
            checks["count"] += 1
            return checks["count"] == 2

        with mock.patch.object(process_supervisor.time, "sleep") as sleep:
            process_supervisor._wait_for_release(released)
        sleep.assert_called_once_with(.025)

    def test_launcher_sigkill_reaps_a_preworker_owned_probe_group(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            code = (
                "import sys,time;sys.path.insert(0,sys.argv[2]);"
                "import local_worker as lw;from pathlib import Path;"
                "tracker=lw.ActiveGroupTracker(Path(sys.argv[1]),'preworker');tracker.begin();"
                "lw._ACTIVE_GROUP_TRACKER.set(tracker);"
                "process=lw.spawn_owned_popen(['/bin/sleep','300']);"
                "print('ready',flush=True);time.sleep(300)"
            )
            launcher = subprocess.Popen([sys.executable, "-c", code, str(state),
                                         str(Path(lw.__file__).parent)],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                self.assertEqual(launcher.stdout.readline().strip(), "ready")
                launcher.stdout.close()
                record = active / "preworker-groups.json"
                payload = json.loads(record.read_text(encoding="utf-8"))
                group = payload["process_groups"][0]
                launcher.kill()
                launcher.wait(timeout=3)
                result = lw.reap_orphans(state)
                self.assertEqual([item["worker_pgid"] for item in result], [group["pgid"]])
                self.assertFalse(any(item["state"] and
                                     not item["state"].startswith("Z")
                                     for item in process_supervisor.process_group_members(
                                         group["pgid"])))
                self.assertFalse(record.exists())
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait()

    def test_reaps_owned_group_after_its_leader_exits(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            child_code = "import subprocess,time; subprocess.Popen(['/bin/sleep','300']); time.sleep(.15)"
            leader = process_supervisor.popen([sys.executable, "-c", child_code])
            pgid = leader.pid
            owner_start = process_supervisor.process_start_identity(pgid)
            process_supervisor.release(leader)
            owned_members = owned_group_snapshot(pgid)
            try:
                time.sleep(.3)
                self.assertIsNone(leader.poll())  # keeper remains until the orphaned child exits
                record = active / "leader-exited.json"
                record.write_text(json.dumps({
                    "job_id": "leader-exited", "launcher_pid": 99999999,
                    "worker_pgid": pgid, "worker_command": sys.executable,
                    "owner_pid": pgid, "owner_sid": os.getsid(pgid),
                    "owner_start_identity": owner_start,
                }))
                # Legacy leader identity is stale after exit. The record's supervisor ownership
                # evidence must still let the launcher reap only this group.
                killed = lw.reap_orphans(state)
                self.assertEqual([item["worker_pgid"] for item in killed], [pgid])
                leader.wait(timeout=2)  # reap our direct child; the keeper had already killed it
                self.assertFalse(lw._pid_alive(pgid))
            finally:
                if lw._pid_alive(pgid):
                    kill_members_with_matching_birth_tokens(owned_members)

    def test_interrupted_job_releases_lease_for_the_next_job(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            options = SimpleNamespace(
                maximum=1,
                task=SimpleNamespace(task_id="interrupted-job", repo_path=Path(root)),
                state_dir=state,
            )
            report = {"job_id": "interrupted-job"}
            with self.assertRaises(lw.Interrupted):
                with lw.job_lease(options, report, timeout=1.0):
                    raise lw.Interrupted("test signal")

            next_report = {"job_id": "next-job"}
            options.task = SimpleNamespace(task_id="next-job", repo_path=Path(root))
            with lw.job_lease(options, next_report, timeout=1.0):
                self.assertEqual(next_report["lease"]["task_id"], "next-job")

    def test_reaps_only_dead_launcher_with_matching_owner_birth_and_session(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            orphan = process_supervisor.popen(["/bin/sleep", "300"])
            identity = process_supervisor.process_start_identity(orphan.pid)
            process_supervisor.release(orphan)
            owned_members = owned_group_snapshot(orphan.pid)
            owned_members = owned_group_snapshot(orphan.pid)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": "/bin/sleep",
                "owner_pid": orphan.pid, "owner_sid": os.getsid(orphan.pid),
                "owner_start_identity": identity,
                "started_at": "now",
            }))
            try:
                killed = lw.reap_orphans(state)
                self.assertEqual([item["worker_pgid"] for item in killed], [orphan.pid])
                self.assertIsNotNone(orphan.wait(10))
                self.assertFalse(record.exists())
            finally:
                if orphan.poll() is None:
                    orphan.kill()
                    orphan.wait()

    def test_does_not_kill_when_pid_birth_identity_does_not_match(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            orphan = process_supervisor.popen(["/bin/sleep", "300"])
            process_supervisor.release(orphan)
            time.sleep(.1)
            owned_members = owned_group_snapshot(orphan.pid)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": "/bin/sleep",
                "owner_pid": orphan.pid, "owner_sid": os.getsid(orphan.pid),
                "owner_start_identity": "not-the-current-birth-token",
                "started_at": "now",
            }))
            try:
                result = lw.reap_orphans(state)
                self.assertEqual([item["action"] for item in result],
                                 ["ownership_uncertain_preserved"])
                self.assertIsNone(orphan.poll())
                self.assertTrue(record.exists())
            finally:
                kill_members_with_matching_birth_tokens(owned_members)
                orphan.wait(timeout=3)

    def test_process_table_failure_preserves_owned_group_without_signalling(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            orphan = process_supervisor.popen(["/bin/sleep", "300"])
            identity = process_supervisor.process_start_identity(orphan.pid)
            process_supervisor.release(orphan)
            time.sleep(.1)
            owned_members = owned_group_snapshot(orphan.pid)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": "/bin/sleep",
                "owner_pid": orphan.pid, "owner_sid": os.getsid(orphan.pid),
                "owner_start_identity": identity,
            }))
            real_run = subprocess.run

            def ps_failure(argv, *args, **kwargs):
                if argv and str(argv[0]).endswith("/ps"):
                    return subprocess.CompletedProcess(argv, 1, "", "ps unavailable")
                return real_run(argv, *args, **kwargs)

            try:
                with mock.patch.object(process_supervisor.subprocess, "run", side_effect=ps_failure):
                    result = lw.reap_orphans(state)
                self.assertEqual(result[0]["action"], "ownership_uncertain_preserved")
                self.assertIsNone(orphan.poll())
                self.assertTrue(record.exists())
            finally:
                kill_members_with_matching_birth_tokens(owned_members)
                orphan.wait(timeout=3)

    def test_process_table_failure_after_term_never_escalates_by_pid_only(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"
            orphan = process_supervisor.popen([sys.executable, "-c", code])
            identity = process_supervisor.process_start_identity(orphan.pid)
            process_supervisor.release(orphan)
            time.sleep(.1)
            owned_members = owned_group_snapshot(orphan.pid)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": sys.executable,
                "owner_pid": orphan.pid, "owner_sid": os.getsid(orphan.pid),
                "owner_start_identity": identity,
            }))
            real_members = process_supervisor.process_group_members
            observations = [real_members(orphan.pid), None]

            def members(_pgid):
                return observations.pop(0) if observations else None

            try:
                with mock.patch.object(process_supervisor, "process_group_members",
                                       side_effect=members):
                    result = lw.reap_orphans(state)
                self.assertEqual(result[0]["action"], "ownership_uncertain_preserved")
                self.assertIsNone(orphan.poll())
                self.assertTrue(record.exists())
            finally:
                kill_members_with_matching_birth_tokens(owned_members)
                orphan.wait(timeout=3)

    def test_escalation_keeps_the_verified_keeper_as_group_owner_through_term(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"
            orphan = process_supervisor.popen([sys.executable, "-c", code])
            identity = process_supervisor.process_start_identity(orphan.pid)
            process_supervisor.release(orphan)
            time.sleep(.1)
            owned_members = owned_group_snapshot(orphan.pid)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": sys.executable,
                "owner_pid": orphan.pid, "owner_sid": os.getsid(orphan.pid),
                "owner_start_identity": identity,
            }))
            try:
                result = lw.reap_orphans(state)
                self.assertEqual([item["worker_pgid"] for item in result], [orphan.pid])
                self.assertEqual(result[0]["action"], "killed")
                self.assertIsNotNone(orphan.wait(timeout=3))
                self.assertFalse(record.exists())
            finally:
                kill_members_with_matching_birth_tokens(owned_members)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    orphan.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
