"""Native gate behavior with a filesystem-real, process-fake runner boundary."""
import importlib
import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
try:
    nr = importlib.import_module('native_readiness')
except ModuleNotFoundError:
    nr = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp
import apple_verification as apple
import local_worker as lw

UDID = '12345678-1234-1234-1234-123456789ABC'
OTHER = '87654321-4321-4321-4321-CBA987654321'
BUNDLE = 'org.example.NativeProbe'


class Budget:
    left = 30

    def remaining(self):
        return self.left


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(nr, 'native readiness gate must exist before model execution')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'worktree'
        self.report = self.root / 'report'
        self.work.mkdir()
        self.report.mkdir()
        self.plan_file = self.root / 'plan.json'
        self.app = self.work / 'build' / 'Probe.app'
        self.result = self.work / 'results.xcresult'
        self.budget = Budget()
        self.calls = []
        self.fail_stage = None
        self.launch_output = BUNDLE + ': 321\n'
        self.make_app = True
        self.actual_bundle = BUNDLE
        self.summary = {
            'title': 'NativeProbe', 'environmentDescription': 'tvOS Simulator',
            'topInsights': [], 'result': 'Passed', 'totalTestCount': 1,
            'passedTests': 1, 'failedTests': 0, 'skippedTests': 0,
            'expectedFailures': 0, 'statistics': [], 'testFailures': [],
            'runtimeWarnings': [],
            'devicesAndConfigurations': [{
                'device': {'deviceId': UDID, 'deviceName': 'TV', 'architecture': 'arm64',
                           'modelName': 'Apple TV', 'osVersion': '26.0'},
                'testPlanConfiguration': {'configurationId': '1', 'configurationName': 'Default'},
                'passedTests': 1, 'failedTests': 0, 'skippedTests': 0, 'expectedFailures': 0,
            }],
        }
        self.plan = {
            'version': 1, 'bundle_id': BUNDLE,
            'app_bundle': 'build/Probe.app', 'result_bundle': 'results.xcresult',
            'build': ['/usr/bin/xcodebuild', '-project', '{worktree}/Probe.xcodeproj',
                      '-scheme', 'Probe', '-destination', 'id={simulator_udid}', 'build'],
            'install': ['/usr/bin/xcrun', 'simctl', 'install', '{simulator_udid}', '{app_bundle}'],
            'launch': ['/usr/bin/xcrun', 'simctl', 'launch', '{simulator_udid}', '{bundle_id}'],
            'test': ['/usr/bin/xcodebuild', '-project', '{worktree}/Probe.xcodeproj',
                     '-scheme', 'Probe', '-destination', 'id={simulator_udid}',
                     '-resultBundlePath', '{result_bundle}', 'test'],
        }

    def runner(self, argv):
        self.calls.append(argv)
        if argv[0].endswith('xcodebuild') and argv[-1] == 'build':
            stage = 'build'
        elif argv[1:3] == ['simctl', 'install']:
            stage = 'install'
        elif argv[1:3] == ['simctl', 'launch'] or argv[0].endswith('/open'):
            stage = 'launch'
        elif argv == ['/bin/ps', '-axo', 'pid=,command=']:
            stage = 'launch_process'
        elif argv[1:2] == ['xcresulttool']:
            stage = 'summary'
        else:
            stage = 'test'
        if self.fail_stage == stage:
            return {'returncode': 65, 'stdout': '', 'stderr': 'native stage failure'}
        if stage == 'build' and self.make_app:
            if self.plan.get('destination') == {'macos': True}:
                (self.app / 'Contents' / 'MacOS').mkdir(parents=True)
                (self.app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(
                    {'CFBundleIdentifier': self.actual_bundle, 'CFBundleExecutable': 'Probe'}))
                (self.app / 'Contents' / 'MacOS' / 'Probe').write_text('binary')
            else:
                self.app.mkdir(parents=True)
                (self.app / 'Info.plist').write_bytes(plistlib.dumps(
                    {'CFBundleIdentifier': self.actual_bundle}))
        if stage == 'test':
            self.result.mkdir()
        output = self.launch_output if stage == 'launch' else ''
        if stage == 'launch_process':
            output = f'432 {self.app}/Contents/MacOS/Probe\n'
        if stage == 'summary':
            output = json.dumps(self.summary)
        return {'returncode': 0, 'stdout': output.encode(), 'stderr': b'', 'timed_out': False}

    def qualify(self, runner=None):
        self.plan_file.write_text(json.dumps(self.plan))
        destination = self.plan.get('destination', {'simulator': UDID})
        return nr.qualify(self.plan_file, self.work, self.report, destination,
                          runner or self.runner, self.budget)

    def rejected(self, pattern, runner=None):
        with self.assertRaisesRegex(nr.NativeReadinessError, pattern) as raised:
            self.qualify(runner)
        evidence = raised.exception.evidence
        self.assertFalse(evidence['qualified'])
        persisted = json.loads((self.report / 'native-readiness.json').read_text())
        self.assertEqual(persisted, evidence)
        return evidence

    def test_real_stage_evidence_qualifies_and_is_persisted(self):
        evidence = self.qualify()
        self.assertTrue(evidence['qualified'])
        self.assertEqual(evidence['simulator_udid'], UDID)
        self.assertEqual(evidence['launch_pid'], 321)
        self.assertEqual(evidence['xctest']['passed'], 1)
        self.assertEqual(self.calls[-1], ['/usr/bin/xcrun', 'xcresulttool', 'get',
            'test-results', 'summary', '--path', str(self.result), '--compact'])
        self.assertEqual(json.loads((self.report / 'native-readiness.json').read_text()), evidence)

    def test_metadata_only_build_success_does_not_qualify(self):
        self.make_app = False
        self.rejected('app bundle')
        self.assertEqual(len(self.calls), 1)

    def test_missing_plan_stops_before_any_native_command(self):
        with self.assertRaises(nr.NativeReadinessError):
            nr.qualify(None, self.work, self.report, UDID, self.runner, self.budget)
        self.assertEqual(self.calls, [])

    def test_zero_or_skipped_only_tests_cannot_qualify(self):
        self.summary.update(passedTests=0, skippedTests=1)
        self.rejected('passing')

    def test_failed_tests_cannot_qualify_despite_zero_process_exit(self):
        self.summary.update(failedTests=1, totalTestCount=2)
        self.rejected('failed')

    def test_unknown_summary_result_is_honest_failure(self):
        self.summary['result'] = 'unknown'
        self.rejected('result')

    def test_other_device_summary_does_not_qualify_selected_device(self):
        self.summary['devicesAndConfigurations'][0]['device']['deviceId'] = OTHER
        self.rejected('device')

    def test_selected_device_must_itself_have_passing_tests(self):
        self.summary['devicesAndConfigurations'][0]['passedTests'] = 0
        self.rejected('device')

    def test_missing_per_device_evidence_fails_closed(self):
        self.summary.pop('devicesAndConfigurations')
        self.rejected('device')

    def test_wrong_built_bundle_id_stops_before_install(self):
        self.actual_bundle = 'org.example.Other'
        self.rejected('bundle identifier')
        self.assertEqual(len(self.calls), 1)

    def test_stale_artifacts_are_rejected_before_build(self):
        for path in (self.app, self.result):
            with self.subTest(path=path):
                path.mkdir(parents=True)
                self.rejected('already exists')
                path.rmdir()
        self.assertEqual(self.calls, [])

    def test_all_failed_stages_stop_immediately_with_logs(self):
        for index, stage in enumerate(('build', 'install', 'launch', 'test', 'summary')):
            with self.subTest(stage=stage):
                self.fail_stage = stage
                self.calls.clear()
                import shutil
                shutil.rmtree(self.app, ignore_errors=True)
                shutil.rmtree(self.result, ignore_errors=True)
                evidence = self.rejected(stage)
                self.assertEqual(len(self.calls), index + 1)
                self.assertIn('native stage failure', Path(evidence['checks'][-1]['log']).read_text())

    def test_launch_requires_real_positive_pid_for_expected_bundle(self):
        self.launch_output = 'simulator ready\n' + BUNDLE + ': 0\n'
        self.rejected('launch.*PID')
        self.assertEqual(len(self.calls), 3)

    def test_timeout_never_becomes_native_success(self):
        def timeout(argv):
            return {'returncode': 0, 'stdout': '', 'stderr': '', 'timed_out': True}
        self.rejected('timeout', timeout)

    def test_expired_shared_budget_stops_before_any_commands(self):
        self.budget.left = 0
        self.rejected('budget')
        self.assertEqual(self.calls, [])

    def test_shell_and_other_device_commands_are_not_proof(self):
        for field, argv in (
            ('build', ['/bin/sh', '-c', 'xcodebuild build']),
            ('install', ['/usr/bin/xcrun', 'simctl', 'install', OTHER, '{app_bundle}']),
            ('launch', ['/usr/bin/xcrun', 'simctl', 'launch', UDID, 'org.example.Other']),
            ('test', ['/bin/echo', 'tests passed']),
        ):
            with self.subTest(field=field):
                original = self.plan[field]
                self.plan[field] = argv
                self.rejected(field)
                self.plan[field] = original
        self.assertEqual(self.calls, [])

    def test_test_destination_cannot_override_selected_device(self):
        self.plan['test'].extend(['-destination', 'id=' + OTHER])
        self.rejected('destination')
        self.assertEqual(self.calls, [])

    def test_artifact_escape_and_symlink_fail_before_commands(self):
        (self.work / 'link').symlink_to(self.root, target_is_directory=True)
        for path in ('../outside.app', '/', '', 'link/escape.app'):
            with self.subTest(path=path):
                self.plan['app_bundle'] = path
                self.rejected('path|symlink|empty|app_bundle')
        self.assertEqual(self.calls, [])

    def test_command_output_path_escape_fails_before_commands(self):
        self.plan['build'].extend(['-derivedDataPath', '/outside/cache'])
        self.rejected('path')
        self.assertEqual(self.calls, [])

    def test_malformed_summary_is_failure_with_preserved_output(self):
        original = self.runner
        def malformed(argv):
            result = original(argv)
            if len(self.calls) == 5:
                result['stdout'] = b'not JSON'
            return result
        evidence = self.rejected('summary', malformed)
        self.assertIn('not JSON', Path(evidence['checks'][-1]['log']).read_text())

    def test_evidence_symlink_is_not_followed_or_raw_exception(self):
        outside = self.root / 'outside.json'
        outside.write_text('preserve')
        (self.report / 'native-readiness.json').symlink_to(outside)
        self.budget.left = 0
        with self.assertRaises(nr.NativeReadinessError) as raised:
            self.qualify()
        self.assertFalse(raised.exception.evidence['qualified'])
        self.assertEqual(outside.read_text(), 'preserve')

    def test_scheme_named_build_cannot_replace_real_build_action(self):
        self.plan['build'] = ['/usr/bin/xcodebuild', '-scheme', 'build',
                              '-destination', 'id=' + UDID, '-showBuildSettings']
        self.rejected('build')
        self.assertEqual(self.calls, [])

    def test_scheme_named_test_cannot_replace_real_test_action(self):
        self.plan['test'] = ['/usr/bin/xcodebuild', '-scheme', 'test',
                             '-destination', 'id=' + UDID,
                             '-resultBundlePath', '{result_bundle}']
        self.rejected('test')
        self.assertEqual(self.calls, [])

    def test_inline_test_selectors_before_action_do_not_hide_test_action(self):
        selectors = ['-only-testing:ProbeUITests/ProbeUITests/testExpectedValue',
                     '-skip-testing:ProbeUITests/ProbeUITests/testUnrelated']
        self.plan['test'] = ['/usr/bin/xcodebuild', '-project', '{worktree}/Probe.xcodeproj',
                             '-scheme', 'Probe', '-destination', 'id={simulator_udid}',
                             '-resultBundlePath', '{result_bundle}', *selectors, 'test']

        evidence = self.qualify()

        self.assertTrue(evidence['qualified'])
        test_call = next(argv for argv in self.calls if argv[0].endswith('xcodebuild') and
                         'test' in argv and '-resultBundlePath' in argv)
        for selector in selectors:
            self.assertIn(selector, test_call)

    def test_callback_exception_records_failure_and_stops(self):
        def unavailable(argv):
            raise OSError('sandbox unavailable')
        evidence = self.rejected('sandbox unavailable', unavailable)
        self.assertEqual(evidence['failed_stage'], 'build')
        self.assertIn('sandbox unavailable', Path(evidence['checks'][-1]['log']).read_text())

    def test_result_created_before_xctest_is_not_accepted(self):
        original = self.runner
        def stale(argv):
            result = original(argv)
            if len(self.calls) == 1:
                self.result.mkdir()
            return result
        self.rejected('already exists', stale)
        self.assertEqual(len(self.calls), 3)

    def test_boolean_test_counts_cannot_stand_for_integers(self):
        self.summary['passedTests'] = True
        self.rejected('counts')

    def test_missing_result_artifact_cannot_be_replaced_by_summary_output(self):
        original = self.runner
        def missing(argv):
            result = original(argv)
            if len(self.calls) == 4:
                self.result.rmdir()
            return result
        self.rejected('result bundle', missing)
        self.assertEqual(len(self.calls), 4)

    def test_plan_version_and_extra_fields_are_rejected(self):
        self.plan['version'] = True
        self.rejected('version')
        self.plan['version'] = 1
        self.plan['sandbox'] = 'unconfined'
        self.rejected('fields')
        self.assertEqual(self.calls, [])

    def test_any_simulator_platform_is_accepted(self):
        for runtime in ("com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                        "com.apple.CoreSimulator.SimRuntime.tvOS-27-0",
                        "com.apple.CoreSimulator.SimRuntime.watchOS-12-0",
                        "com.apple.CoreSimulator.SimRuntime.xrOS-3-0"):
            with self.subTest(runtime=runtime):
                devices = sp.simctl_devices({runtime: [{"udid": sp.APPLE_UDID,
                                                        "isAvailable": True}]})
                self.assertTrue(nr.platform_for_udid(sp.APPLE_UDID, devices))

    def test_v1_plan_upgrades_to_a_simulator_destination(self):
        self.plan_file.write_text(json.dumps(self.plan))
        plan = nr._load(self.plan_file, self.work, self.report, {'simulator': UDID})
        self.assertEqual(plan["destination"], {"simulator": UDID})
        self.assertEqual(plan["version"], 2)

    def test_macos_destination_skips_install(self):
        self.plan = {
            "version": 2, "destination": {"macos": True}, "bundle_id": BUNDLE,
            "app_bundle": "build/Probe.app", "result_bundle": "results.xcresult",
            "build": ["xcodebuild", "-project", "{worktree}/Probe.xcodeproj",
                      "-scheme", "Probe", "-destination", "platform=macOS", "build"],
            "launch": ["open", "-a", "{app_bundle}"],
            "test": ["xcodebuild", "-project", "{worktree}/Probe.xcodeproj",
                     "-scheme", "Probe", "-destination", "platform=macOS",
                     "-resultBundlePath", "{result_bundle}", "test"],
        }
        evidence = self.qualify()
        self.assertEqual([c["name"] for c in evidence["checks"]],
                         ["build", "launch", "launch_process", "test", "summary"])
        self.assertEqual(evidence["launch_pid"], 432)
        self.assertEqual(evidence["launch_executable"],
                         str(self.app / "Contents" / "MacOS" / "Probe"))

    def test_macos_open_success_without_matching_running_process_fails(self):
        self.plan = {
            "version": 2, "destination": {"macos": True}, "bundle_id": BUNDLE,
            "app_bundle": "build/Probe.app", "result_bundle": "results.xcresult",
            "build": ["xcodebuild", "-project", "{worktree}/Probe.xcodeproj",
                      "-scheme", "Probe", "-destination", "platform=macOS", "build"],
            "launch": ["open", "-a", "{app_bundle}"],
            "test": ["xcodebuild", "-project", "{worktree}/Probe.xcodeproj",
                     "-scheme", "Probe", "-destination", "platform=macOS",
                     "-resultBundlePath", "{result_bundle}", "test"],
        }
        def no_process(argv):
            result = self.runner(argv)
            if argv == ["/bin/ps", "-axo", "pid=,command="]:
                result["stdout"] = b"432 /Applications/Other.app/Contents/MacOS/Other\n"
            return result
        self.rejected("no positive PID", no_process)

    def test_legacy_confinement_is_gone(self):
        self.assertFalse(hasattr(lw, "sandbox_argv"))
        self.assertFalse(hasattr(lw, "_probe_codex_sandbox"))
        self.assertFalse(hasattr(apple, "build_profile"))

    def test_documented_single_device_object_is_accepted(self):
        self.summary['devicesAndConfigurations'] = self.summary['devicesAndConfigurations'][0]
        self.assertTrue(self.qualify()['qualified'])

    def test_stage_wall_time_is_measured_on_success(self):
        from unittest import mock
        with mock.patch.object(nr.time,'monotonic',side_effect=range(10,20)):
            evidence=self.qualify()
        self.assertEqual([c['elapsed_seconds'] for c in evidence['checks']],[1.0]*5)

    def test_stage_wall_time_is_retained_when_callback_raises(self):
        from unittest import mock
        def failed(argv): raise OSError('socket denied')
        with mock.patch.object(nr.time,'monotonic',side_effect=[10,12]):
            evidence=self.rejected('socket denied',failed)
        self.assertEqual(evidence['checks'][0]['elapsed_seconds'],2)


if __name__ == '__main__':
    unittest.main()
