"""Signal handling and safe cleanup of worker groups left by crashed launchers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_worker as lw  # noqa: E402


class OrphanReaping(unittest.TestCase):
    def test_reaps_only_dead_launcher_with_matching_unwrapped_command(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            orphan = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": "/bin/sleep",
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

    def test_does_not_kill_when_group_leader_command_does_not_match(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            active = state / "active"
            active.mkdir()
            orphan = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
            record = active / "job.json"
            record.write_text(json.dumps({
                "job_id": "job", "launcher_pid": 99999999,
                "worker_pgid": orphan.pid, "worker_command": "/bin/not-sleep",
                "started_at": "now",
            }))
            try:
                self.assertEqual(lw.reap_orphans(state), [])
                self.assertIsNone(orphan.poll())
                self.assertTrue(record.exists())
            finally:
                orphan.terminate()
                orphan.wait()


if __name__ == "__main__":
    unittest.main()
