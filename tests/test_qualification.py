'''Offline tests for the deterministic qualification controller. No model or Codex call.'''
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import qualification  # noqa: E402


class ForbiddenExecution:
    '''Any subprocess attempt fails the test: dry/report/audit must launch nothing.'''

    def __call__(self, *args, **kwargs):
        raise AssertionError('qualification must not launch a subprocess here')


@contextlib.contextmanager
def no_subprocess():
    forbidden = ForbiddenExecution()
    with mock.patch.object(qualification.subprocess, 'run', forbidden), \
            mock.patch.object(qualification.subprocess, 'Popen', forbidden), \
            mock.patch.object(qualification.subprocess, 'check_output', forbidden), \
            mock.patch.object(qualification.subprocess, 'check_call', forbidden):
        yield forbidden


class QualificationTests(unittest.TestCase):
    def test_main_live_nonzero_stage_exits_nonzero_and_keeps_receipts(self):
        output = self.root / 'main-live-campaign'
        calls = []

        def fake_execute(argv, cwd, env, log_path):
            calls.append((list(argv), Path(log_path)))
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text('stage output\n')
            return 3 if len(calls) == 3 else 0

        stream = self.out()
        with no_subprocess():
            exit_code = qualification.main(['run', '--live', '--output-dir', str(output),
                                            '--source', str(self.source)], stdout=stream,
                                           execute=fake_execute)
        self.assertEqual(exit_code, 1, 'a nonzero stage must never be reported as success')
        self.assertEqual(len(calls), 3, 'the campaign stops at the failure and is not restarted')
        self.assertIn('stopped at account (exit 3)', stream.getvalue())
        receipts = json.loads((output / qualification.STAGE_RECEIPTS_NAME).read_text())
        self.assertEqual(receipts['stage_exit_codes'], {'setup': 0, 'run': 0, 'account': 3})
        self.assertEqual([entry['stage'] for entry in receipts['stages']],
                         ['setup', 'run', 'account'])
        self.assertEqual([entry['returncode'] for entry in receipts['stages']], [0, 0, 3])
        for entry in receipts['stages']:
            self.assertTrue(Path(entry['log_path']).is_file(), 'each stage keeps its log receipt')
        run_receipt = json.loads((output / qualification.RUN_RECEIPT_NAME).read_text())
        self.assertEqual(run_receipt['failed_stage'], 'account')
        self.assertTrue(run_receipt['incomplete'])
        self.assertFalse(run_receipt['report_rendered'])
        self.assertFalse((output / 'report.md').exists(),
                         'a campaign that did not pass every stage gets no report')
        self.assertFalse((output / 'stage-logs' / 'finalize.log').exists())

    def test_live_success_renders_report_from_this_campaign_evidence(self):
        output = self.root / 'success-campaign'
        rows = [
            {'task': 'slug', 'arm': 'direct_cloud', 'status': 'accepted', 'uncached_input': 1200,
             'cached_input': 300, 'output': 150, 'total_cloud_tokens': 1650, 'seconds': 11.5},
            {'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 100,
             'cached_input': 1300, 'output': 60, 'total_cloud_tokens': 1460, 'seconds': 21.25},
            {'task': 'slug', 'arm': 'direct_luna', 'status': 'rejected', 'uncached_input': 900,
             'cached_input': 400, 'output': 120, 'total_cloud_tokens': 1420, 'seconds': 33.0},
        ]
        seen = []

        def fake_execute(argv, cwd, env, log_path):
            stage = Path(argv[1]).stem
            seen.append(stage)
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text('ok\n')
            if stage == 'account':
                (Path(cwd) / qualification.RESULTS_NAME).write_text(json.dumps(rows))
            return 0

        stream = self.out()
        with no_subprocess():
            result = qualification.run_campaign(str(output), live=True, source=str(self.source),
                                                execute=fake_execute, stdout=stream)
        self.assertEqual(seen, list(qualification.STAGES))
        self.assertIsNone(result['failed_stage'])
        self.assertFalse(result['incomplete'])
        self.assertTrue(result['report_rendered'])
        report = (output / 'report.md').read_text()
        self.assertEqual(report, qualification.build_report(str(output)))
        for figure in ('1,460', '1,650', '1,420', '21.2', '11.5'):
            self.assertIn(figure, report)
        self.assertIn('Measured task-arm cloud total is 4,530', report)
        for stale in ('84,628', '83,858.5', '42,881', '86,781', '160,999', '69.314', '30.795',
                      '40.187', '50.6%', '9.5%'):
            self.assertNotIn(stale, report)
            self.assertNotIn(stale, (output / 'finalize.py').read_text())
            self.assertNotIn(stale, qualification.template_path('finalize').read_text())

    def test_report_overall_total_is_unavailable_when_any_arm_total_is_missing(self):
        rows = [
            {'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 100,
             'cached_input': 900, 'output': 50, 'total_cloud_tokens': 1050, 'seconds': 10.0},
            {'task': 'stats', 'arm': 'direct_cloud', 'status': 'accepted', 'uncached_input': 700,
             'cached_input': 200, 'output': 80, 'total_cloud_tokens': None, 'seconds': 12.0},
        ]
        with no_subprocess():
            report = qualification.build_report(str(self.results_file(rows)))
        measured = [line for line in report.splitlines()
                    if 'Measured task-arm cloud total' in line]
        self.assertTrue(measured, report)
        self.assertIn('unavailable', measured[0])
        self.assertIn('direct_cloud', measured[0])
        self.assertIn('known subtotal 1,050', report)
        self.assertNotIn('cloud total is 1,050.', report)
        self.assertIn('Rows with missing usage fields: 1.', report)

    def test_report_never_infers_a_total_from_one_missing_category(self):
        rows = [{'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 80,
                 'cached_input': None, 'output': 40, 'total_cloud_tokens': 520, 'seconds': 20.0}]
        with no_subprocess():
            report = qualification.build_report(str(self.results_file(rows)))
        self.assertIn('520', report, 'the recorded total is kept exactly as recorded')
        self.assertIn('Measured task-arm cloud total is 520', report)
        self.assertIn('Rows with missing usage fields: 1.', report)

    def test_report_refuses_a_total_that_is_not_the_sum_of_its_categories(self):
        rows = [{'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 100,
                 'cached_input': 900, 'output': 50, 'total_cloud_tokens': 150, 'seconds': 10.0}]
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.build_report(str(self.results_file(rows)))
        self.assertIn('is not uncached_input+cached_input+output', str(caught.exception))
        self.assertIn('never reconciled by guessing', str(caught.exception))

    def test_live_refuses_a_source_without_git_metadata_before_any_write(self):
        installed = self.root / 'installed-cache'
        for relative in ('bin/local-worker', 'src/local_worker.py',
                         'examples/matched-benchmark'):
            path = installed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# installed copy, not a checkout\n')
        output = self.root / 'gitless-campaign'
        with no_subprocess():
            plan = qualification.run_campaign(str(output), source=str(installed),
                                              stdout=self.out())
            self.assertEqual(plan['mode'], 'dry-run')
            self.assertFalse(output.exists(), 'a dry run needs no git subprocess or auth')
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.run_campaign(str(output), live=True, source=str(installed),
                                           stdout=self.out())
        self.assertIn('use --source', str(caught.exception))
        self.assertFalse(output.exists(), 'nothing is created, staged or executed')
        self.assertEqual(sorted(path.name for path in installed.iterdir()),
                         ['bin', 'examples', 'src'])
        worktree = self.root / 'linked-worktree'
        for relative in ('bin/local-worker', 'src/local_worker.py',
                         'examples/matched-benchmark'):
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# linked worktree\n')
        (worktree / '.git').write_text('gitdir: /elsewhere/commondir\n')
        live_output = self.root / 'worktree-campaign'

        def fake_execute(argv, cwd, env, log_path):
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text('ok\n')
            (Path(cwd) / qualification.RESULTS_NAME).write_text(json.dumps(
                [{'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 10,
                  'cached_input': 20, 'output': 5, 'total_cloud_tokens': 35, 'seconds': 1.0}]))
            return 0

        with no_subprocess():
            result = qualification.run_campaign(str(live_output), live=True,
                                                source=str(worktree), execute=fake_execute,
                                                stdout=self.out())
        self.assertEqual(result['stage_exit_codes'], {stage: 0 for stage in qualification.STAGES})
        self.assertTrue((live_output / 'report.md').is_file())

    def test_symlinked_output_parent_resolving_inside_a_checkout_is_refused(self):
        inside = [str(entry) for entry in sorted(self.source.iterdir())]
        source_link = self.root / 'source-link'
        source_link.symlink_to(self.source, target_is_directory=True)
        checkout_link = self.root / 'checkout-link'
        checkout_link.symlink_to(qualification.REPO_ROOT, target_is_directory=True)
        for label, output in (
                ('qualification source checkout',
                 source_link / 'evidence' / 'fresh-campaign'),
                ('addon checkout', checkout_link / 'evidence' / 'fresh-campaign')):
            for live in (False, True):
                with no_subprocess():
                    with self.assertRaises(qualification.QualificationError) as caught:
                        qualification.run_campaign(str(output), live=live,
                                                   source=str(self.source), stdout=self.out())
                self.assertIn(label, str(caught.exception))
                self.assertFalse(output.is_symlink() or output.exists())
        self.assertEqual([str(entry) for entry in sorted(self.source.iterdir())], inside,
                         'a refusal must not write anything into the source checkout')
        dangling = self.root / 'dangling-link'
        dangling.symlink_to(self.root / 'never-created')
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.run_campaign(str(dangling), source=str(self.source),
                                           stdout=self.out())
        self.assertIn('existing campaign', str(caught.exception))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source-checkout'
        for relative in ('bin/local-worker', 'src/local_worker.py',
                         'examples/matched-benchmark'):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# frozen fixture\n')
        (self.source / '.git').mkdir()  # fake checkout metadata; no git subprocess is run

    @staticmethod
    def out():
        return io.StringIO()

    def results_file(self, rows, aggregate=None):
        evidence = self.root / 'evidence'
        evidence.mkdir(exist_ok=True)
        (evidence / 'per-task-results.json').write_text(json.dumps(rows))
        if aggregate is not None:
            (evidence / 'aggregate.json').write_text(json.dumps(aggregate))
        return evidence

    def test_dry_run_plans_without_writing_or_launching(self):
        output = self.root / 'campaign'
        stream = self.out()
        with no_subprocess():
            plan = qualification.run_campaign(str(output), source=str(self.source), stdout=stream)
            exit_code = qualification.main(['run', '--output-dir', str(output),
                                           '--source', str(self.source)], stdout=self.out())
        printed = stream.getvalue()
        self.assertFalse(plan['executed'])
        self.assertEqual(plan['mode'], 'dry-run')
        self.assertEqual(plan['stage_exit_codes'], {})
        self.assertFalse(output.exists(), 'a dry run must not create the evidence directory')
        self.assertEqual(exit_code, 0)
        for expected in (str(self.source), str(output), 'implementation_seconds',
                         'local_internal_attempts', 'review_seconds', 'planning_seconds'):
            self.assertIn(expected, printed)
        for stage in qualification.STAGES:
            self.assertIn(json.dumps([sys.executable, str(output / (stage + '.py'))]), printed)

    def test_run_refuses_existing_source_contained_and_relative_output(self):
        existing = self.root / 'already-a-campaign'
        existing.mkdir()
        (existing / 'setup.py').write_text('# prior evidence\n')
        for live in (False, True):
            with no_subprocess():
                with self.assertRaises(qualification.QualificationError) as caught:
                    qualification.run_campaign(str(existing), live=live, source=str(self.source),
                                               stdout=self.out())
            self.assertIn('existing campaign', str(caught.exception))
        self.assertEqual((existing / 'setup.py').read_text(), '# prior evidence\n')
        inside_checkout = qualification.REPO_ROOT / 'evidence' / 'new-campaign'
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.run_campaign(str(inside_checkout), source=str(self.source),
                                           stdout=self.out())
        self.assertIn('addon checkout', str(caught.exception))
        self.assertFalse(inside_checkout.exists())
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.run_campaign('relative/campaign', source=str(self.source),
                                           stdout=self.out())
        self.assertIn('absolute', str(caught.exception))
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.run_campaign(str(self.root / 'campaign'), live=True,
                                           source=str(self.root / 'nowhere'), stdout=self.out())
        self.assertIn('source checkout is missing', str(caught.exception))
        self.assertFalse((self.root / 'campaign').exists())

    def test_live_run_stages_each_stage_once_and_keeps_nonzero_exit_evidence(self):
        output = self.root / 'live-campaign'
        calls = []

        def fake_execute(argv, cwd, env, log_path):
            calls.append((list(argv), Path(cwd), dict(env), Path(log_path)))
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text('stage output\n')
            return 3 if len(calls) == 3 else 0

        stream = self.out()
        with mock.patch.object(qualification, 'execute_stage', fake_execute):
            result = qualification.run_campaign(str(output), live=True, source=str(self.source),
                                                stdout=stream)
        staged = [output / (stage + '.py') for stage in qualification.STAGES]
        self.assertEqual([call[0][1] for call in calls], [str(path) for path in staged[:3]],
                         'stages run once each, sequentially, stopping at the failure')
        self.assertEqual(len(calls), 3, 'a failed stage must not be retried or restarted')
        self.assertEqual(result['stage_exit_codes'], {'setup': 0, 'run': 0, 'account': 3})
        self.assertEqual(result['failed_stage'], 'account')
        self.assertTrue(result['incomplete'])
        for call in calls:
            self.assertEqual(call[2][qualification.SOURCE_ENV], str(self.source))
            self.assertTrue(call[3].is_file(), 'each stage keeps a durable log')
        for path, stage in zip(staged, qualification.STAGES):
            self.assertEqual(path.read_text(), qualification.template_path(stage).read_text())
        receipts = json.loads((output / qualification.STAGE_RECEIPTS_NAME).read_text())
        self.assertEqual(receipts['stage_exit_codes']['account'], 3)
        self.assertFalse((output / 'stage-logs' / 'finalize.log').exists(),
                         'finalize must not run after a failed stage')
        self.assertIn('stopped at account (exit 3)', stream.getvalue())
        self.assertIn(str(output / 'report.md'), stream.getvalue())

    def test_report_charges_rejected_work_and_marks_missing_usage_unknown(self):
        rows = [
            {'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 100,
             'cached_input': 900, 'output': 50, 'total_cloud_tokens': 1050, 'seconds': 10.0},
            {'task': 'slug', 'arm': 'launcher', 'status': 'rejected', 'uncached_input': 80,
             'cached_input': None, 'output': 40, 'total_cloud_tokens': 520, 'seconds': 20.0},
            {'task': 'stats', 'arm': 'launcher', 'status': 'failed', 'uncached_input': 5,
             'cached_input': 5, 'output': 5, 'total_cloud_tokens': 15, 'seconds': 1.0},
        ]
        evidence = self.results_file(rows)
        with no_subprocess():
            report = qualification.build_report(str(evidence))
            rendered = self.out()
            exit_code = qualification.main(['report', '--evidence-dir', str(evidence)],
                                           stdout=rendered)
        self.assertEqual(exit_code, 0)
        self.assertEqual(rendered.getvalue(), report)
        rejected = next(line for line in report.splitlines() if '| rejected |' in line)
        failed = next(line for line in report.splitlines() if '| failed |' in line)
        self.assertEqual(rejected.split('|')[5].strip(), 'unavailable',
                         'missing cached input must stay unknown, never zero')
        self.assertIn('520', rejected.replace(',', ''))
        self.assertIn('15', failed.replace(',', ''))
        accounting = [line for line in report.splitlines() if line.startswith('| launcher |')]
        self.assertEqual(len(accounting), 1)
        self.assertIn('| 3 | 1 | 1 | 1 |', accounting[0])
        self.assertIn('1,585.0', accounting[0],
                      'rejected and failed work stays charged to the completed-task cost')
        self.assertIn('- Rows with missing usage fields: 1.', report,
                      'unknown usage is counted as unknown, not as zero')

    def test_report_fails_clearly_on_incomplete_or_invalid_evidence(self):
        cases = [
            ('missing per-task results', lambda: qualification.build_report(str(self.root))),
            ('non-empty list', lambda: qualification.build_report(str(self.results_file([])))),
            ('missing', lambda: qualification.build_report(str(self.results_file(
                [{'task': 'slug', 'arm': 'launcher'}])))),
            ('unknown status', lambda: qualification.build_report(str(self.results_file(
                [{'task': 'slug', 'arm': 'launcher', 'status': 'maybe', 'uncached_input': 1,
                  'cached_input': 1, 'output': 1, 'total_cloud_tokens': 3, 'seconds': 1.0}])))),
            ('never guessed', lambda: qualification.build_report(str(self.results_file(
                [{'task': 'slug', 'arm': 'launcher', 'status': 'accepted',
                  'uncached_input': 'lots', 'cached_input': 1, 'output': 1,
                  'total_cloud_tokens': 2, 'seconds': 1.0}])))),
            ('disagrees', lambda: qualification.build_report(str(self.results_file(
                [{'task': 'slug', 'arm': 'launcher', 'status': 'accepted', 'uncached_input': 1,
                  'cached_input': 1, 'output': 1, 'total_cloud_tokens': 3, 'seconds': 1.0}],
                aggregate={'launcher': {'attempted_tasks': 5, 'completed_tasks': 5,
                                        'rejected_tasks': 0, 'failed_tasks': 0,
                                        'total_cloud_tokens_including_rejections': 99}})))),
        ]
        for expected, call in cases:
            with self.subTest(expected=expected):
                with no_subprocess():
                    with self.assertRaises(qualification.QualificationError) as caught:
                        call()
                self.assertIn(expected, str(caught.exception))
        rendered = self.out()
        with no_subprocess():
            exit_code = qualification.main(['report', '--evidence-dir', str(self.root)],
                                           stdout=rendered)
        self.assertEqual(exit_code, 2)
        self.assertIn('missing per-task results', rendered.getvalue())

    def write_snapshot(self, counts=None):
        evidence = self.root / 'parent'
        evidence.mkdir(exist_ok=True)
        baseline = {'input_tokens': 1000, 'cached_input_tokens': 800, 'output_tokens': 10,
                    'total_tokens': 1010, 'reasoning_output_tokens': 4,
                    'cache_write_input_tokens': 0}
        records = [
            {'timestamp': '2026-10-26T00:01:00.000Z', 'total_token_usage':
                {'input_tokens': 1300, 'cached_input_tokens': 1000, 'output_tokens': 30,
                 'total_tokens': 1330, 'reasoning_output_tokens': 12,
                 'cache_write_input_tokens': 0}},
            {'timestamp': '2026-10-26T00:02:00.000Z', 'total_token_usage':
                {'input_tokens': 1700, 'cached_input_tokens': 1400, 'output_tokens': 50,
                 'total_tokens': 1750, 'reasoning_output_tokens': 20,
                 'cache_write_input_tokens': 0}},
            {'timestamp': '2026-10-26T00:03:00.000Z', 'total_token_usage':
                {'input_tokens': 2200, 'cached_input_tokens': 1900, 'output_tokens': 80,
                 'total_tokens': 2280, 'reasoning_output_tokens': 30,
                 'cache_write_input_tokens': 0}},
        ]
        snapshot = {'completeness': 'snapshot', 'request_timestamp': '2026-10-26T00:00:00.000Z',
                    'as_of': records[-1]['timestamp'], 'baseline_total_usage': baseline,
                    'counts': counts if counts is not None else
                    {'input_tokens': 1200, 'cached_input_tokens': 1100, 'output_tokens': 70,
                     'total_tokens': 1270},
                    'scope': 'parent chat orchestration only'}
        path = evidence / qualification.SNAPSHOT_NAME
        path.write_text(json.dumps(snapshot))
        (evidence / qualification.COUNTER_RECORDS_NAME).write_text(json.dumps(records))
        return path

    def test_audit_reconciles_fixture_cumulative_counters(self):
        path = self.write_snapshot()
        with no_subprocess():
            audit = qualification.audit_parent_overhead(str(path))
            rendered = self.out()
            exit_code = qualification.main(['audit', '--snapshot', str(path)], stdout=rendered)
        self.assertEqual(audit['status'], 'reconciled')
        self.assertTrue(all(audit['checks'].values()), audit['checks'])
        self.assertEqual(audit['responses'], 3)
        self.assertEqual(audit['first_input_delta_from_baseline'], 300)
        self.assertEqual(audit['median_input_per_response'], 400)
        self.assertEqual(audit['input_tokens'], 1200)
        self.assertEqual(audit['new_input_tokens'], 100)
        self.assertEqual(audit['reasoning_output_tokens'], 26)
        self.assertEqual(audit['visible_output_tokens'], 44)
        self.assertEqual(audit['total_tokens'], 1270)
        self.assertEqual(audit['private_reasoning_bodies'], 'not read')
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(rendered.getvalue())['median_input_per_response'], 400)

    def test_audit_flags_mismatch_and_missing_inputs(self):
        path = self.write_snapshot(counts={'input_tokens': 1201, 'cached_input_tokens': 1100,
                                          'output_tokens': 70, 'total_tokens': 1271})
        with no_subprocess():
            audit = qualification.audit_parent_overhead(str(path))
            rendered = self.out()
            exit_code = qualification.main(['audit', '--snapshot', str(path)], stdout=rendered)
        self.assertEqual(audit['status'], 'mismatch')
        self.assertFalse(audit['checks']['input_delta_matches_counts'])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(rendered.getvalue())['status'], 'mismatch')
        (path.with_name(qualification.COUNTER_RECORDS_NAME)).unlink()
        with no_subprocess():
            with self.assertRaises(qualification.QualificationError) as caught:
                qualification.audit_parent_overhead(str(path))
        self.assertIn('counter records', str(caught.exception))

    def test_staged_templates_compile_and_carry_no_personal_paths_or_thread_ids(self):
        problems = qualification.check_templates()
        self.assertEqual({stage: issues for stage, issues in problems.items() if issues}, {})
        for stage in qualification.STAGES:
            text = qualification.template_path(stage).read_text()
            compile(text, stage, 'exec')
            self.assertIn('LOCAL_WORKER_QUALIFICATION_SOURCE', text)
            self.assertIn('R=Path(__file__).parent', text)
            self.assertNotIn('/Users/', text)
            self.assertNotIn('Run comparasion.', text)
        for stage in ('setup', 'run'):
            self.assertIn("BIN=SRC/'bin/local-worker'",
                          qualification.template_path(stage).read_text())
        self.assertIn('parent={}', qualification.template_path('finalize').read_text(),
                      'outer overhead stays unavailable, never zero')
        leaked = "import pathlib\nX=pathlib.Path('/Users/someone/x')\n"
        leaked += "T='01a17552-0678-75a3-8cd7-379ea8d95c79'\n"
        self.assertEqual(qualification.portability_problems(leaked),
                         ['personal absolute path', 'hardcoded thread/rollout id'])

    def test_environment_source_override_drives_the_plan(self):
        output = self.root / 'env-campaign'
        with no_subprocess():
            with mock.patch.dict(os.environ, {qualification.SOURCE_ENV: str(self.source)}):
                plan = qualification.build_plan(str(output))
        self.assertEqual(plan['source'], str(self.source))
        self.assertEqual(plan['source_env'], qualification.SOURCE_ENV)
        self.assertEqual(plan['budgets'], qualification.BUDGETS)
        self.assertEqual([tuple(pair) for pair in plan['execution_order']],
                         list(qualification.EXECUTION_ORDER))
        self.assertEqual(len(plan['execution_order']), 6)


if __name__ == '__main__':
    unittest.main()
