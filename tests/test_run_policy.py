"""Persistent convergence and repair budgets: real state, no model calls."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import run_policy as policy

class RunPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
    def make(self, kind='implementation', repo='repo', stream='feature'):
        return policy.Workstream(self.root, repo, stream, kind)
    def test_task_renaming_cannot_restart_two_empty_attempts(self):
        a=self.make(); a.claim('task-a', 60, 1); a.record_attempt('task-a', has_patch=False); a.finish('task-a', 10)
        b=self.make(); b.claim('task-b', 60, 1); b.record_attempt('task-b', has_patch=False); b.finish('task-b', 10)
        with self.assertRaises(policy.PolicyStop): self.make().claim('task-c', 60, 1)
    def test_scoped_patch_restarts_empty_streak_but_not_repair_budget(self):
        a=self.make('repair'); a.claim('a', 60, 1); a.record_attempt('a', has_patch=False); a.finish('a', 8)
        a.claim('b', 60, 1); a.record_attempt('b', has_patch=True); a.finish('b', 7)
        with self.assertRaises(policy.PolicyStop): a.claim('c', 60, 1)
    def test_repair_claims_reserve_attempts_and_time_before_concurrent_work(self):
        a=self.make('repair'); first=a.claim('a', 800, 1)
        self.assertEqual(first['job_budget_seconds'], 800)
        b=self.make('repair'); second=b.claim('b', 800, 1)
        self.assertEqual(second['job_budget_seconds'], 400)
        with self.assertRaises(policy.PolicyStop): self.make('repair').claim('c', 60, 1)
        a.finish('a', 20); b.finish('b', 20)
    def test_overrun_and_repeat_record_cannot_refund_repair_budget(self):
        a=self.make('repair'); a.claim('a', 1200, 1); a.record_attempt('a',has_patch=True); a.finish('a',1201); a.finish('a',0)
        with self.assertRaises(policy.PolicyStop): a.claim('b',60,1)
    def test_implementation_cannot_bypass_exhausted_repair_window(self):
        a=self.make('repair');a.claim('a',1200,2);a.record_attempt('a',has_patch=True);a.record_attempt('a',has_patch=True);a.finish('a',1)
        with self.assertRaises(policy.PolicyStop):self.make().claim('b',60,1)
    def test_independent_repository_or_workstream_is_not_blocked(self):
        a=self.make();a.claim('a',60,2);a.record_attempt('a',has_patch=False);a.record_attempt('a',has_patch=False);a.finish('a',1)
        self.make(repo='other').claim('b',60,1)
        self.make(stream='new-user-task').claim('c',60,1)
    def test_corrupt_state_fails_closed(self):
        a=self.make(); a.path.parent.mkdir(parents=True,exist_ok=True);a.path.write_text('{broken')
        with self.assertRaises(policy.PolicyStop): a.claim('a',60,1)
    def test_blank_workstream_is_refused(self):
        for value in ['', ' ', '../escape']:
            with self.subTest(value=value), self.assertRaises(ValueError):self.make(stream=value)
    def test_started_repair_is_charged_even_when_no_result_is_returned(self):
        a=self.make('repair');a.claim('a',60,2);a.start_attempt('a');a.finish('a',1)
        a.claim('b',60,2);a.start_attempt('b');a.finish('b',1)
        with self.assertRaises(policy.PolicyStop):a.claim('c',60,1)
    def test_corrupt_active_reservations_cannot_expand_budget(self):
        import json
        a=self.make('repair');a.claim('a',60,1)
        state=json.loads(a.path.read_text());state['active']['a']['reserved_seconds']=-10000;a.path.write_text(json.dumps(state))
        with self.assertRaises(policy.PolicyStop):self.make('repair').claim('b',1200,1)
