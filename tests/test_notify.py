import os
import sys
import subprocess
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import notify


class NotifyTests(unittest.TestCase):
    def test_message_travels_as_argv_and_is_bounded(self):
        calls = []
        with mock.patch.dict(os.environ, {"LOCAL_WORKER_NOTIFY": "1"}):
            result = notify.notify("job", 'Job "J1"', "x" * 500 + "SECRET_VALUE",
                                   run=lambda argv, **kwargs: calls.append((argv, kwargs)) or
                                   subprocess.CompletedProcess(argv, 0))
        self.assertTrue(result)
        argv, _kwargs = calls[0]
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("item 2 of argv", argv[4])
        self.assertEqual(argv[-2], 'Job "J1"')
        self.assertLessEqual(len(argv[-1]), 200)

    def test_disabled_and_failing_notifications_never_raise(self):
        with self.assertLogs(notify._log, level="WARNING") as captured:
            with mock.patch.dict(os.environ, {"LOCAL_WORKER_NOTIFY": "0"}):
                self.assertFalse(notify.notify("job", "t", "m", run=self.fail_if_called))
            with mock.patch.dict(os.environ, {"LOCAL_WORKER_NOTIFY": "1"}):
                self.assertFalse(notify.notify("job", "t", "m",
                                               run=lambda *a, **k: (_ for _ in ()).throw(OSError("no"))))
                self.assertFalse(notify.notify("job", "t", "m", run=lambda *a, **k:
                                               subprocess.CompletedProcess([], 1)))
        self.assertEqual(len(captured.output), 2)
        self.assertTrue(all("macOS notification failed" in message
                            for message in captured.output))

    def test_notification_caller_redacts_before_sending(self):
        import local_worker
        sent = []
        secret = "plausible-private-token"
        report = {"job_id": "J1", "status": "blocked", "failure_reason": secret,
                  "classification": {"code": "deny_list_unavailable", "next_action": "needs_user",
                                     "remediation": "Restore the deny wrapper.",
                                     "plugin_suggestion": secret},
                  "permissions": {"deny_list_applied": False, "reason": secret}}
        with mock.patch.object(local_worker.notify, "notify", side_effect=lambda *args, **kwargs:
                               sent.append(args) or True):
            local_worker._notify_job(report, [secret])
        self.assertTrue(sent)
        self.assertNotIn(secret, repr(sent))
        self.assertIn("Deny list was not applied", sent[0][2])

    def test_summary_command_notifies_after_writing(self):
        import local_worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                              "LOCAL_WORKER_NOTIFY": "0"}):
                with mock.patch.object(local_worker.notify, "notify", return_value=True) as sent:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = local_worker.main(["summary", "--session", "S1",
                                                    "--state-dir", str(state)])
        self.assertEqual(result, 0)
        self.assertEqual(sent.call_args.args[0], "summary")
        self.assertIn("written", sent.call_args.args[2])

    def fail_if_called(self, *_args, **_kwargs):
        self.fail("disabled notifier called subprocess")


if __name__ == "__main__":
    unittest.main()
