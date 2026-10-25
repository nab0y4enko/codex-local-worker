"""Real git/filesystem checks of the cloud semantic review boundary."""
import json
import io
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp
import review_workflow as rw


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.state = self.root / 'state'
        self.job = 'review-example'
        self.worktree = self.state / 'worktrees' / self.job
        sp.lw.add_worktree(self.repo, sp.lw.rev_parse(self.repo, 'HEAD'), self.worktree)
        (self.worktree / 'add.py').write_text(sp.FIX_ADD)
        self.directory = self.state / 'reports' / self.job
        self.directory.mkdir(parents=True)
        patch = self.directory / 'changes.patch'
        patch.write_bytes(sp.git(self.worktree, 'diff', '--binary', 'HEAD').stdout)
        self.report = dict(job_id=self.job, repo_path=str(self.repo), worktree=str(self.worktree),
            report_dir=str(self.directory), base_commit=sp.lw.rev_parse(self.repo, 'HEAD'),
            status='ok', kind='write', violations=[], changed_paths=['add.py'],
            tests=[dict(command=[sys.executable, '-c', 'import add; assert add.add(2, 3) == 5'], status='passed')],
            patch_path=str(patch), review_findings=[], primary_unchanged=True,
            contract=dict(task_id='example', repo_path=str(self.repo), base_ref='main', goal='fix add',
                acceptance_criteria=['addition works'], allowed_paths=['add.py'],
                test_commands=[[sys.executable, '-c', 'import add; assert add.add(2, 3) == 5']],
                timeout_seconds=60, max_attempts=1))
        self.save()

    def save(self):
        self.report['worker_fingerprint_after'] = sp.lw.primary_fingerprint(self.worktree)
        (self.directory / 'report.json').write_text(json.dumps(self.report))

    def package(self):
        return rw.build_package(self.state, self.job)

    def decision(self, package):
        return dict(action='accept', package_sha256=package['package_sha256'],
                    reviewer='cloud-planner', semantic_review=True,
                    reviewed_sections=['diff', 'sources', 'tests', 'findings', 'audit', 'screenshots'],
                    rationale='Checked addition implementation and independently verified behavior.')

    def test_package_contains_complete_diff_and_requires_cloud_review(self):
        package = self.package()
        self.assertTrue(package['complete'])
        self.assertIn('+    return a + b', package['patch']['text'])
        self.assertTrue(package['semantic_review_required'])
        self.assertEqual(package['checks']['tests'][0]['status'], 'passed')
        self.assertIn('add.py', package['sources'])

    def test_worker_changes_since_verification_cannot_be_packaged_as_complete(self):
        (self.worktree / 'add.py').write_text('def add(a, b):\n    return 999\n')
        self.assertFalse(self.package()['complete'])

    def read_report(self):
        (self.worktree / 'add.py').write_text(sp.BASE_FILES['add.py'])
        self.report.update(kind='read', changed_paths=[], tests=[], patch_path=None,
            answer='The function subtracts b from a.',
            citations=[{'path': 'add.py', 'line_start': 2, 'line_end': 2,
                        'quote': '    return a - b'}],
            citation_verification={'total': 1, 'verified': 1, 'failures': []},
            read_audit={'changed_paths': [], 'violations': [], 'no_changes_required': True},
            contract=dict(task_id='example', kind='read', repo_path=str(self.repo),
                base_ref='main', goal='inspect add', timeout_seconds=60, question='What does add do?',
                scope_paths=['add.py'], answer_schema={'type': 'string'}, max_answer_bytes=1024))
        self.save()

    def test_read_package_contains_citations_source_and_audit(self):
        self.read_report()
        package = self.package()
        self.assertTrue(package['complete'], package['incomplete_reasons'])
        self.assertEqual(package['checks']['citation_verification']['verified'], 1)
        self.assertTrue(package['checks']['read_audit']['no_changes_required'])
        self.assertIn('return a - b', package['sources']['add.py']['text'])
        self.assertEqual(len(package['checks']['citations']), 1)

    def test_read_without_citation_proof_cannot_be_accepted(self):
        self.read_report()
        self.report['citation_verification']['verified'] = 0
        self.save()
        self.assertFalse(self.package()['complete'])

    def test_changed_patch_refuses_before_primary_is_modified(self):
        package = self.package()
        Path(self.report['patch_path']).write_text('corrupt')
        with self.assertRaisesRegex(rw.WorkflowError, 'changed'):
            rw.decide(self.state, self.job, self.decision(package))
        self.assertIn('a - b', (self.repo / 'add.py').read_text())

    def test_cloud_semantic_review_is_required(self):
        package = self.package()
        decision = self.decision(package)
        decision['semantic_review'] = False
        with self.assertRaisesRegex(rw.WorkflowError, 'semantic'):
            rw.decide(self.state, self.job, decision)

    def test_patch_file_race_never_applies_unreviewed_bytes(self):
        package = self.package()
        original = sp.lw.git
        changed = [False]
        def racing_git(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ('apply', '--check') and not changed[0]:
                changed[0] = True
                path = Path(self.report['patch_path'])
                path.write_bytes(path.read_bytes().replace(b'+    return a + b', b'+    return 999'))
            return result
        with mock.patch.object(sp.lw, 'git', racing_git), mock.patch.dict('os.environ', sp.GIT_ID):
            result = rw.decide(self.state, self.job, self.decision(package))
        self.assertNotIn('return 999', (self.repo / 'add.py').read_text())
        self.assertEqual(result['status'], 'integration_failed')
        self.assertTrue(self.worktree.exists())

    def test_changed_checkout_invalidates_decision(self):
        package = self.package()
        (self.repo / 'notes.md').write_text('new user changes')
        with self.assertRaisesRegex(rw.WorkflowError, 'checkout'):
            rw.decide(self.state, self.job, self.decision(package))
        self.assertEqual((self.repo / 'notes.md').read_text(), 'new user changes')

    def test_missing_tests_and_truncated_patch_cannot_be_accepted(self):
        self.report['tests'] = []
        self.save()
        package = rw.build_package(self.state, self.job, max_patch_bytes=8)
        self.assertFalse(package['complete'])
        self.assertTrue(package['patch']['truncated'])
        with self.assertRaisesRegex(rw.WorkflowError, 'incomplete'):
            rw.decide(self.state, self.job, self.decision(package))

    def test_success_integrates_runs_checks_and_archives_with_real_receipt(self):
        package = self.package()
        with mock.patch.dict('os.environ', sp.GIT_ID):
            result = rw.decide(self.state, self.job, self.decision(package))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['integration_tests'][0]['status'], 'passed')
        self.assertTrue(result['archive']['removed'])
        self.assertFalse(self.worktree.exists())
        self.assertIn('a + b', (self.repo / 'add.py').read_text())

    def test_failed_combined_check_preserves_patch_and_worktree(self):
        package = self.package()
        decision = self.decision(package)
        decision['combined_commands'] = [[sys.executable, '-c', 'raise SystemExit(1)']]
        result = rw.decide(self.state, self.job, decision)
        self.assertEqual(result['status'], 'integration_failed')
        self.assertTrue(result['applied'])
        self.assertTrue(self.worktree.exists())
        self.assertIn('a + b', (self.repo / 'add.py').read_text())
        self.assertFalse((self.state / 'archives' / self.job).exists())

    def test_unrelated_existing_dirty_work_is_preserved(self):
        (self.repo / 'notes.md').write_text('keep user work')
        package = self.package()
        with mock.patch.dict('os.environ', sp.GIT_ID):
            result = rw.decide(self.state, self.job, self.decision(package))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual((self.repo / 'notes.md').read_text(), 'keep user work')

    def test_dirty_target_is_refused(self):
        (self.repo / 'add.py').write_text('user content')
        package = self.package()
        with self.assertRaisesRegex(rw.WorkflowError, 'overlap'):
            rw.decide(self.state, self.job, self.decision(package))

    def trusted_checks(self, source):
        script = self.root / 'trusted.py'
        script.write_text(source)
        manifest = self.root / 'trusted.json'
        manifest.write_text(json.dumps({'version': 1, 'checks': [
            {'name': 'independent', 'argv': [sys.executable, str(script.resolve())],
             'scripts': [str(script.resolve())]}]}))
        pinned = rw.acceptance_checks.load_manifest(manifest, repo=self.repo)
        self.report['contract'].update(kind='write', acceptance_checks=str(manifest.resolve()))
        self.report['acceptance_checks'] = {'status': 'passed', 'input_pins': [
            {'path': str(p.path), 'sha256': p.sha256, 'device': p.device, 'inode': p.inode}
            for p in pinned.pins]}
        self.save()
        return script

    def test_independent_acceptance_checks_are_rerun_on_integrated_checkout(self):
        self.trusted_checks('raise SystemExit(7)\n')
        package = self.package()
        result = rw.decide(self.state, self.job, self.decision(package))
        self.assertEqual(result['status'], 'integration_failed')
        self.assertEqual(result['integration_tests'][-1]['name'], 'independent')
        self.assertEqual(result['integration_tests'][-1]['returncode'], 7)
        self.assertTrue(self.worktree.exists())

    def test_trusted_script_change_after_review_blocks_before_integration(self):
        script = self.trusted_checks('pass\n')
        package = self.package()
        script.write_text('raise SystemExit(1)\n')
        with self.assertRaisesRegex(rw.WorkflowError, 'changed'):
            rw.decide(self.state, self.job, self.decision(package))
        self.assertIn('a - b', (self.repo / 'add.py').read_text())

    def test_trusted_script_change_before_packaging_is_not_requalified(self):
        script = self.trusted_checks('pass\n')
        script.write_text('raise SystemExit(1)\n')
        package = self.package()
        self.assertFalse(package['complete'])
        self.assertIn('Trusted acceptance inputs changed', ' '.join(package['incomplete_reasons']))

    def test_integration_check_cannot_write_primary_source(self):
        package = self.package()
        decision = self.decision(package)
        decision['combined_commands'] = [[sys.executable, '-c',
            "from pathlib import Path; Path('notes.md').write_text('destroyed')"]]
        result = rw.decide(self.state, self.job, decision)
        self.assertEqual(result['status'], 'integration_failed')
        self.assertEqual((self.repo / 'notes.md').read_text(), 'base notes\n')

    def test_queue_executes_pinned_snapshot_not_mutable_original(self):
        path = self.root / 'task.json'
        path.write_text(json.dumps(self.report['contract']))
        manifest = self.root / 'queue.json'
        manifest.write_text(json.dumps({'tasks': [str(path)]}))
        launched = []
        def run(argv, **kwargs):
            snapshot = Path(argv[argv.index('--task-file') + 1])
            launched.append(snapshot)
            path.write_text('{}')
            payload = json.loads(snapshot.read_text())
            self.assertEqual(payload['task_id'], 'example')
            return sp.subprocess.CompletedProcess(argv, 1, '{}', '')
        result = rw.orchestrate(manifest, self.state, run=run)
        self.assertNotEqual(launched[0], path)
        self.assertEqual(result['input_pins'][0]['contract']['task_id'], 'example')

    def test_integration_refuses_without_permission_wrapper(self):
        package = self.package()
        with mock.patch.object(rw.permissions, 'wrapper_available', return_value=False):
            result = rw.decide(self.state, self.job, self.decision(package))
        self.assertEqual(result['status'], 'integration_failed')
        self.assertIn('wrapper', result['reason'])
        self.assertTrue(self.worktree.exists())

    def test_queue_timeout_retains_pending_tasks_and_receipt(self):
        path = self.root / 'task.json'
        path.write_text(json.dumps(self.report['contract']))
        manifest = self.root / 'queue.json'
        manifest.write_text(json.dumps({'tasks': [str(path), str(path)]}))
        def timeout(argv, **kwargs):
            raise sp.subprocess.TimeoutExpired(argv, 60)
        result = rw.orchestrate(manifest, self.state, run=timeout)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['pending_tasks'], [str(path)])
        self.assertTrue(Path(result['receipt_path']).is_file())
        self.assertEqual(result['jobs'][0]['status'], 'launcher_timeout')

    def test_queue_to_explicit_cloud_decision_with_real_launcher_reports(self):
        payload = dict(self.report['contract'])
        path = self.root / 'task.json'
        path.write_text(json.dumps(payload))
        manifest = self.root / 'queue.json'
        manifest.write_text(json.dumps({'tasks': [str(path)]}))
        settings = sp.write_settings(self.root / 'settings.json')
        def run(argv, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(sp.lw, 'run_bounded',
                    sp.FakeRun(worker_edit=sp.fix_add)):
                code = sp.lw.main(argv[2:], omlx_fetch=sp.FakeOmlx('one'), codex_run=sp.fake_help())
            return sp.subprocess.CompletedProcess(argv, code, output.getvalue(), '')
        result = rw.orchestrate(manifest, self.state, run=run,
                                 launcher_args=['--settings', str(settings), '--codex-bin', 'codex-mock'])
        self.assertEqual(result['status'], 'awaiting_cloud_review', result)
        self.assertEqual(len(result['jobs']), 1)
        job = result['jobs'][0]['job_id']
        package = rw.read_json(result['jobs'][0]['package_path'])
        self.assertTrue(package['complete'])
        self.assertIn('a - b', (self.repo / 'add.py').read_text())
        with mock.patch.dict('os.environ', sp.GIT_ID):
            accepted = rw.decide(self.state, job, self.decision(package))
        self.assertEqual(accepted['status'], 'accepted', accepted)
        self.assertTrue(accepted['archive']['removed'])
        self.assertIn('a + b', (self.repo / 'add.py').read_text())

    def test_queue_stops_on_failure_and_never_resets_retry_budget(self):
        paths = []
        for index in range(2):
            path = self.root / ('task%d.json' % index)
            path.write_text(json.dumps(dict(self.report['contract'], task_id='task%d' % index)))
            paths.append(str(path))
        manifest = self.root / 'queue.json'
        manifest.write_text(json.dumps({'tasks': paths}))
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return sp.subprocess.CompletedProcess(argv, 1, json.dumps({'status':'worker_failed'}), '')
        result = rw.orchestrate(manifest, self.state, run=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['pending_tasks'], paths[1:])


if __name__ == '__main__':
    unittest.main()
