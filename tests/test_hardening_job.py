import json
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import support as sp
lw=sp.lw

class HardenedJobTests(unittest.TestCase):
    def setUp(self):
        self.root=sp.tmpdir();self.addCleanup(sp.cleanup,self.root);self.repo=sp.make_repo(self.root)
    def run_fake(self, options, fake):
        with mock.patch.object(lw,'run_bounded',fake):return lw.run_job(options)
    def test_worker_cannot_spend_verification_reserve(self):
        fake=sp.FakeRun(worker_edit=lambda cwd,n:(cwd/'add.py').write_text('fixed\n'))
        report=self.run_fake(sp.job_options(self.root,self.repo),fake)
        worker=next(c for c in fake.calls if c['argv'][1:2]==['exec'])
        self.assertLess(worker['timeout'],55)
        self.assertGreater(report['attempts'][0]['verification_reserve_seconds'],0)
    def test_two_no_patch_jobs_stop_before_third_worker_or_worktree(self):
        for i in range(2):
            fake=sp.FakeRun(worker_rc=1); report=self.run_fake(sp.job_options(self.root,self.repo),fake);self.assertEqual(fake.exec_calls,1)
        fake=sp.FakeRun(); report=self.run_fake(sp.job_options(self.root,self.repo),fake)
        self.assertEqual(report['status'],'blocked');self.assertEqual(fake.exec_calls,0);self.assertIsNone(report['worktree'])
    def test_apple_requires_end_to_end_plan_before_model_and_rejects_skip(self):
        for verify in (True,False):
            options=sp.job_options(self.root,self.repo,verify=verify,apple_udid=sp.APPLE_UDID)
            options.native_preflight_plan=None
            fake=sp.FakeRun();report=self.run_fake(options,fake)
            self.assertEqual(report['status'],'blocked');self.assertEqual(fake.exec_calls,0)
            self.assertIn('native',report['failure_reason'].lower())
    def test_preflight_only_cannot_return_implementation_success(self):
        options=sp.job_options(self.root,self.repo); options.preflight_only=True
        fake=sp.FakeRun();report=self.run_fake(options,fake)
        self.assertNotEqual(report['status'],'ok');self.assertEqual(fake.exec_calls,0)
    def test_usage_includes_both_attempts(self):
        options=sp.job_options(self.root,self.repo,max_attempts=2)
        def edit(cwd,n):
            if n==2:(cwd/'add.py').write_text('fixed\n')
        fake=sp.FakeRun(worker_rc=1,worker_edit=edit)
        report=self.run_fake(options,fake)
        self.assertEqual(report['token_counts'],{'input_tokens':22,'output_tokens':14})
        self.assertEqual(report['usage']['coverage']['attempts_total'],2)
    def test_report_has_final_progress_and_actual_durations(self):
        fake=sp.FakeRun(worker_edit=lambda cwd,n:(cwd/'add.py').write_text('fixed\n'))
        report=self.run_fake(sp.job_options(self.root,self.repo),fake)
        progress=json.loads((Path(report['report_dir'])/'progress.json').read_text())
        self.assertEqual(progress['status'],report['status'])
        self.assertIn('worker_seconds',report['timing'])
        self.assertIn('verification_seconds',report['timing'])
        self.assertIsNone(report['usage']['cached_input_tokens'])
    def test_old_patch_is_not_fresh_progress_on_retry(self):
        def edit(cwd,n):
            if n==1:(cwd/'add.py').write_text('fixed\n')
        report=self.run_fake(sp.job_options(self.root,self.repo,max_attempts=2),sp.FakeRun(worker_rc=1,worker_edit=edit))
        self.assertEqual([a['has_scoped_patch'] for a in report['attempts']],[True,False])
    def test_orphan_native_plan_is_not_ignored(self):
        options=sp.job_options(self.root,self.repo);options.native_preflight_plan=self.root/'plan.json'
        fake=sp.FakeRun();report=self.run_fake(options,fake)
        self.assertEqual(report['status'],'blocked');self.assertEqual(fake.exec_calls,0)
    def test_interrupted_repair_keeps_attempt_charge(self):
        for i in range(2):
            options=sp.job_options(self.root,self.repo);options.job_kind='repair'
            fake=sp.FakeRun()
            def crash(argv, **kwargs):
                if list(argv)[1:2]==['exec']:raise OSError('worker runner lost')
                return fake(argv,**kwargs)
            with mock.patch.object(lw,'run_bounded',crash):report=lw.run_job(options)
            self.assertIn('worker runner lost',report['failure_reason'])
        fake=sp.FakeRun();report=self.run_fake(options,fake)
        self.assertEqual(report['status'],'blocked');self.assertEqual(fake.exec_calls,0)
    def test_repair_budget_reports_actual_elapsed_after_clipping(self):
        from dataclasses import replace
        options=sp.job_options(self.root,self.repo);options.task=replace(options.task,timeout_seconds=3600);options.job_kind='repair'
        fake=sp.FakeRun(worker_edit=lambda cwd,n:(cwd/'add.py').write_text('fixed\n'))
        report=self.run_fake(options,fake)
        self.assertLessEqual(report['budget']['effective_seconds'],1200.1)
        self.assertAlmostEqual(report['budget']['elapsed_seconds'],report['elapsed_seconds'],places=2)
        self.assertLess(report['budget']['elapsed_seconds'],10)
    def test_status_uses_all_attempt_usage(self):
        options=sp.job_options(self.root,self.repo,max_attempts=2)
        report=self.run_fake(options,sp.FakeRun(worker_rc=1))
        progress=json.loads((Path(report['report_dir'])/'progress.json').read_text())
        self.assertEqual(progress['usage']['input_tokens'],22)
        self.assertEqual(progress['usage']['availability'],'partial')
    def test_interrupted_attempt_has_actual_duration_and_unknown_usage(self):
        options=sp.job_options(self.root,self.repo);fake=sp.FakeRun()
        def crash(argv,**kwargs):
            if list(argv)[1:2]==['exec']:raise OSError('lost child')
            return fake(argv,**kwargs)
        with mock.patch.object(lw,'run_bounded',crash):report=lw.run_job(options)
        self.assertEqual(len(report['attempts']),1)
        self.assertGreaterEqual(report['attempts'][0]['elapsed_seconds'],0)
        self.assertEqual(report['usage']['coverage']['attempts_total'],1)
        self.assertIsNone(report['usage']['input_tokens'])
    def test_dead_launcher_status_is_interrupted(self):
        item={'active':True,'status':'processing','launcher_pid':123456,'elapsed_seconds':2}
        with mock.patch.object(lw.os,'kill',side_effect=ProcessLookupError):result=lw.status_snapshot(item)
        self.assertEqual(result['status'],'interrupted');self.assertFalse(result['active'])
        self.assertEqual(result['liveness_source'],'launcher_pid')
    def test_completed_status_preserves_elapsed(self):
        item={'active':False,'status':'ok','elapsed_seconds':2,'launcher_pid':123456}
        with mock.patch.object(lw.os,'kill') as check:result=lw.status_snapshot(item)
        check.assert_not_called();self.assertEqual(result['elapsed_seconds'],2)
    def test_each_verification_round_retains_logs_and_duration(self):
        report=self.run_fake(sp.job_options(self.root,self.repo,max_attempts=2),sp.FakeRun(worker_rc=1))
        rounds=[a['tests'] for a in report['attempts']]
        paths=[t['log_path'] for ts in rounds for t in ts if t.get('log_path')]
        self.assertEqual(len(paths),len(set(paths)))
        self.assertTrue(all(Path(p).is_file() for p in paths))
        self.assertEqual(report['timing']['verification_seconds'],round(sum(t.get('elapsed_seconds',0) or 0 for ts in rounds for t in ts),3))
    def test_reverting_all_work_does_not_reset_empty_patch_streak(self):
        original=(self.repo/'add.py').read_text()
        def edit(cwd,n):(cwd/'add.py').write_text('changed\n' if n==1 else original)
        report=self.run_fake(sp.job_options(self.root,self.repo,max_attempts=2),sp.FakeRun(worker_rc=1,worker_edit=edit))
        self.assertEqual([a['has_scoped_patch'] for a in report['attempts']],[True,False])
