"""Launcher/native gate seam: real orchestration and filesystem, fake processes.

Strict plan/evidence interpretation lives in test_native_readiness. These tests
exercise the callback actually supplied by the launcher and its worker ordering.
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp

lw = sp.lw


class NativeGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.events = []
        self.native_calls = []
        self.probe_apple = mock.MagicMock(name='probe_apple')
        self.gate_done = False
        self.gate_fail = False
        self.native_timeout = False
        self.plan = self.root / 'caller-plan.json'
        self.plan.write_text('{}')  # Interpreter is injected at this seam.
        self.options = sp.job_options(self.root, self.repo, apple_udid=sp.APPLE_UDID)
        self.options.native_preflight_plan = self.plan
        self.fake = sp.FakeRun(worker_edit=lambda cwd, n: (cwd / 'add.py').write_text(
            'def add(a, b):\n    return a + b\n'))

    def qualify(self, plan_path, worktree, report_dir, udid, run, deadline):
        self.events.append('gate_started')
        self.assertEqual(plan_path, self.plan)
        self.assertEqual(udid, sp.APPLE_UDID)
        self.assertTrue(Path(worktree).is_dir())
        self.assertTrue(report_dir.is_dir())
        # The only profile is the job's deny wrapper; no Apple confinement profile is created.
        self.assertEqual([path.name for path in report_dir.glob('*.sb')], [sp.DENY_PROFILE_NAME])
        self.assertFalse(self.probe_apple.called)              # no readiness probe runs here
        self.assertEqual(self.fake.exec_calls, 0)
        # Call the real full-host closure, rather than treating its existence as proof.
        for stage in ('build', 'install', 'launch', 'test', 'summary'):
            self.events.append('native_' + stage)
            argv = ['/usr/bin/xcodebuild', 'build'] if stage == 'build' else [
                '/usr/bin/xcrun', 'native-seam-fixture', stage, udid]
            before = deadline.remaining()
            result = run(argv)
            call = self.native_calls[-1]
            self.assertEqual(call['argv'], argv)     # the plan's argv, run inside the deny wrapper
            self.assertTrue(call['wrapped'])
            self.assertEqual(call['cwd'], worktree)
            self.assertGreater(call['timeout'], 0)
            self.assertLessEqual(call['timeout'], before)
            self.assertEqual(call['env']['IBToolNeverDeque'], '1')
            self.assertNotIn(lw.apple.FOUNDATION_HOME_ENV, call['env'])
            self.assertNotIn(lw.API_KEY_ENV, call['env'])
            self.assertNotIn(sp.SECRET, str(result))
            if result['returncode'] != 0 or result.get('timed_out'):
                evidence = {'qualified': False, 'status': 'blocked',
                            'failed_stage': stage, 'checks': []}
                raise lw.native.NativeReadinessError('native ' + stage + ' failed', evidence)
        self.gate_done = True
        self.events.append('gate_completed')
        return {'qualified': True, 'status': 'passed', 'simulator_udid': udid,
                'checks': [], 'launch_pid': 101, 'xctest': {'passed': 1, 'failed': 0}}

    def runner(self, argv, *, cwd, env, timeout, stdin_data=None, stall_seconds=None,
               on_stall=None):
        raw = [str(item) for item in argv]
        argv, wrapped = sp.deny_wrapped(raw)
        if wrapped and argv == sp.WRAPPER_PROBE:
            return self.fake(raw, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
                             stall_seconds=stall_seconds, on_stall=on_stall)
        if argv[0] == lw.apple.SANDBOX_EXEC:
            self.fail('full-host native runs must not use the legacy Apple profile')
        if argv == list(lw.FULL_HOST_PROBE_ARGV):
            return self.fake(raw, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
                             stall_seconds=stall_seconds, on_stall=on_stall)
        if argv[1:2] == ['exec']:
            self.assertTrue(self.gate_done, 'worker started before native qualification completed')
            self.assertTrue(wrapped, 'the worker must run inside the deny wrapper')
            self.events.append('worker')
            return self.fake(raw, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
                             stall_seconds=stall_seconds, on_stall=on_stall)
        call = {'argv': list(argv), 'wrapped': wrapped, 'cwd': cwd, 'env': dict(env),
                'timeout': timeout}
        self.native_calls.append(call)
        if not self.gate_done:
            return sp.result(argv, 65 if self.gate_fail else 0,
                             out='native callback output ' + sp.SECRET,
                             timed_out=self.native_timeout)
        self.events.append('verification')
        self.assertTrue(wrapped, 'verification must run inside the deny wrapper')
        self.assertEqual(env['IBToolNeverDeque'], '1')
        self.assertNotIn(lw.apple.FOUNDATION_HOME_ENV, env)
        self.assertNotIn(lw.API_KEY_ENV, env)
        return self.fake(raw, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
                         stall_seconds=stall_seconds, on_stall=on_stall)

    @contextlib.contextmanager
    def injected(self):
        with mock.patch.object(lw.apple, 'probe_apple', self.probe_apple), \
             mock.patch.object(lw.native, 'qualify', self.qualify), \
             mock.patch.object(lw, 'run_bounded', self.runner):
            yield

    def run_job(self):
        with self.injected():
            return lw.run_job(self.options)

    def test_gate_callbacks_share_verification_sandbox_env_and_finish_before_worker(self):
        report = self.run_job()
        self.assertEqual(report['status'], 'ok', report.get('failure_reason'))
        self.assertEqual(self.events, ['gate_started', 'native_build',
            'native_install', 'native_launch', 'native_test', 'native_summary',
            'gate_completed', 'worker', 'verification'])
        self.assertFalse(self.probe_apple.called)
        self.assertEqual(report['sandbox']['backend'], lw.FULL_HOST_BACKEND)
        self.assertIs(report['attempts'][0]['tests'][0]['sandboxed'], False)
        self.assertTrue(report['native_preflight']['qualified'])
        self.assertEqual(self.fake.exec_calls, 1)
        self.assertTrue(report['primary_unchanged'])
        self.assertEqual((self.repo / 'add.py').read_text(), sp.BASE_FILES['add.py'])
        self.assertEqual(len(self.native_calls), 6)
        self.assertNotIn(sp.SECRET, json.dumps(report))

    def test_metadata_pass_cannot_override_native_stage_failure(self):
        self.gate_fail = True
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['exit_code'], 3)
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertEqual(self.events, ['gate_started', 'native_build'])
        self.assertFalse(self.probe_apple.called)
        self.assertEqual([path.name for path in Path(report['report_dir']).glob('*.sb')],
                         [sp.DENY_PROFILE_NAME])
        self.assertFalse(report['native_preflight']['qualified'])
        self.assertEqual(report['attempts'], [])
        self.assertTrue((Path(report['report_dir']) / 'report.json').is_file())

    def test_native_timeout_stops_before_worker(self):
        self.native_timeout = True
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertFalse(report['native_preflight']['qualified'])

    def test_preflight_only_runs_gate_without_implementation_success(self):
        self.options.preflight_only = True
        report = self.run_job()
        self.assertEqual(report['status'], 'ready', report.get('failure_reason'))
        self.assertEqual(report['exit_code'], 0)
        self.assertTrue(report['qualification_only'])
        self.assertTrue(report['native_preflight']['qualified'])
        self.assertEqual(report['attempts'], [])
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertEqual(self.events[-1], 'gate_completed')
        self.assertNotIn('verification', self.events)

    def test_native_plan_missing_stops_before_worker_and_worktree(self):
        self.options.native_preflight_plan = None
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertIsNone(report['worktree'])
        self.assertEqual(self.events, [])

    def test_no_verify_cannot_bypass_present_native_plan(self):
        self.options.verify = False
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertIsNone(report['worktree'])
        self.assertEqual(self.events, [])

    def test_plan_without_simulator_is_rejected_instead_of_ignored(self):
        self.options.apple_simulator_udid = None
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertIsNone(report['worktree'])
        self.assertEqual(self.events, [])

    def test_preflight_only_without_simulator_never_becomes_generic_implementation(self):
        self.options.preflight_only = True
        self.options.apple_simulator_udid = None
        self.options.native_preflight_plan = None
        report = self.run_job()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertEqual(self.events, [])

    def test_cli_preflight_only_returns_ready_json_and_zero_without_worker(self):
        task = self.root / 'task.json'
        task.write_text(json.dumps(sp.task_payload(self.repo)))
        output = io.StringIO()
        with self.injected(), contextlib.redirect_stdout(output):
            status = lw.main(['run', '--task-file', str(task), '--state-dir',
                str(self.options.state_dir), '--settings', str(self.options.settings_path),
                '--codex-bin', sys.executable, '--apple-simulator-udid', sp.APPLE_UDID,
                '--native-preflight-plan', str(self.plan), '--preflight-only', '--json'],
                omlx_fetch=sp.FakeOmlx('one'), codex_run=sp.fake_help())
        self.assertEqual(status, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'ready')
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertTrue(self.gate_done)


if __name__ == '__main__':
    unittest.main()
