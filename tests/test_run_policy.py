"""Tiered, capability-only workstream budgets."""
import json
import contextlib
import io
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import contract
import local_worker as lw
import run_policy as policy
import support as sp


class RunPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make(self, kind="implementation", repo="repo", stream="feature", tier="local"):
        return policy.Workstream(self.root, repo, stream, kind, tier=tier)

    def trip(self, workstream):
        for job in ("a", "b"):
            workstream.claim(job, 60, 1)
            workstream.record_attempt(job, capability_failure=True, has_patch=False)
            workstream.finish(job, 1)

    def test_environment_failures_never_trip_the_breaker(self):
        workstream = self.make()
        for job in ("a", "b", "c"):
            workstream.claim(job, 60, 1)
            workstream.record_attempt(job, capability_failure=False, has_patch=False)
            workstream.finish(job, 1)
        workstream.claim("d", 60, 1)

    def test_local_breaker_escalates_and_cloud_has_an_independent_allowance(self):
        workstream = self.make()
        self.trip(workstream)
        with self.assertRaises(policy.PolicyStop) as ctx:
            workstream.claim("c", 60, 1)
        self.assertEqual(ctx.exception.next_action, "escalate_cloud")
        self.make(tier="cloud").claim("c-cloud", 60, 1)

    def test_cloud_allowance_has_a_user_action_and_is_tier_scoped(self):
        cloud = self.make(tier="cloud")
        for job in ("cloud-a", "cloud-b"):
            cloud.claim(job, 60, 1)
            cloud.start_attempt(job)
            cloud.record_attempt(job, capability_failure=True, has_patch=False)
            cloud.finish(job, 1)
        with self.assertRaises(policy.PolicyStop) as ctx:
            cloud.claim("cloud-c", 60, 1)
        self.assertEqual(ctx.exception.next_action, "needs_user")
        self.make(tier="local").claim("local-still-available", 60, 1)

    def test_renaming_cannot_reset_but_explicit_reset_can(self):
        self.trip(self.make())
        with self.assertRaises(policy.PolicyStop):
            self.make().claim("renamed", 60, 1)
        result = policy.reset(self.root, "repo", "feature", actor="user")
        self.assertTrue(result["reset"])
        self.assertTrue((self.root / "workstreams" / "reset.log").is_file())
        self.make().claim("after-reset", 60, 1)

    def test_default_workstream_is_the_task_id(self):
        temp = sp.tmpdir()
        self.addCleanup(sp.cleanup, temp)
        repo = sp.make_repo(temp)
        self.assertEqual(contract.Contract.from_payload(sp.task_payload(repo)).workstream,
                         "task-1")

    def test_repair_reservations_stay_bounded(self):
        first = self.make(kind="repair")
        self.assertEqual(first.claim("a", 800, 1)["job_budget_seconds"], 800)
        second = self.make(kind="repair")
        self.assertEqual(second.claim("b", 800, 1)["job_budget_seconds"], 400)
        with self.assertRaises(policy.PolicyStop):
            self.make(kind="repair").claim("c", 60, 1)
        first.finish("a", 20)
        second.finish("b", 20)

    def test_corrupt_and_interrupted_reservations_fail_closed(self):
        workstream = self.make(kind="repair")
        workstream.path.parent.mkdir(parents=True, exist_ok=True)
        workstream.path.write_text("{broken")
        with self.assertRaises(policy.PolicyStop):
            workstream.claim("a", 60, 1)

    def test_pre_tier_local_state_migrates_without_refunding_repair_or_breaker_usage(self):
        name, repo_identity = "feature", "repo"
        legacy_key = hashlib.sha256((repo_identity + "\0" + name).encode()).hexdigest()
        legacy_path = self.root / "workstreams" / (legacy_key + ".json")
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(json.dumps({"version": 1, "workstream": name,
            "empty_streak": 1, "repair_attempts": 1, "repair_seconds": 600.0,
            "active": {}}))
        workstream = self.make(kind="repair", repo=repo_identity, stream=name)
        admission = workstream.claim("a", 800, 2)
        self.assertEqual(admission["empty_streak"], 1)
        self.assertEqual(admission["job_budget_seconds"], 600.0)
        workstream.record_attempt("a", capability_failure=True, has_patch=False)
        workstream.finish("a", 1)
        with self.assertRaises(policy.PolicyStop) as ctx:
            self.make(kind="implementation", repo=repo_identity, stream=name).claim("b", 60, 1)
        self.assertEqual(ctx.exception.next_action, "escalate_cloud")

    def test_corrupt_legacy_local_state_fails_closed(self):
        name, repo_identity = "feature", "repo"
        legacy_key = hashlib.sha256((repo_identity + "\0" + name).encode()).hexdigest()
        legacy_path = self.root / "workstreams" / (legacy_key + ".json")
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("{broken")
        with self.assertRaises(policy.PolicyStop):
            self.make(repo=repo_identity, stream=name).claim("job", 60, 1)

    def test_reset_is_logged_with_actor_and_scope(self):
        result = policy.reset(self.root, "repo", "feature", actor="test-operator")
        entry = json.loads((self.root / "workstreams" / "reset.log").read_text().splitlines()[0])
        self.assertEqual(result["actor"], "test-operator")
        self.assertEqual((entry["repo_identity"], entry["workstream"]), ("repo", "feature"))

    def test_cli_workstream_reset_uses_the_repo_and_logs_the_actor(self):
        temp = sp.tmpdir()
        self.addCleanup(sp.cleanup, temp)
        repo = sp.make_repo(temp)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = lw.main(["workstream", "reset", "--repo", str(repo), "--workstream",
                            "feature", "--state-dir", str(self.root), "--actor", "operator"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result["reset"])
        self.assertEqual(result["actor"], "operator")
        self.assertTrue((self.root / "workstreams" / "reset.log").is_file())


if __name__ == "__main__":
    unittest.main()
