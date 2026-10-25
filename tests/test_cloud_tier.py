"""Cloud catalog and escalation tier stay on the ordinary audited job path."""
import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_catalog
import local_worker as lw
import support as sp


class CloudTierTests(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)

    def test_cloud_catalog_is_read_live(self):
        completed = sp.fake_completed(stdout=sp.load_fixture_text("codex/debug_models.json"))
        models = codex_catalog.list_cloud_models("codex", run=lambda *a, **k: completed)
        self.assertIn("gpt-6-luna", [model.slug for model in models])
        self.assertTrue(all(model.description for model in models))
        self.assertEqual([model.slug for model in models], [
            item["slug"] for item in sp.load_fixture("codex/debug_models.json")["models"]
            if item.get("visibility") == "list"])

    def test_cloud_job_uses_the_same_pipeline_and_links_its_origin(self):
        fake = sp.FakeRun(worker_edit=sp.fix_add)
        refreshed = json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "synthetic-access-token-refreshed",
            "refresh_token": "synthetic-refresh-token-refreshed",
            "id_token": "synthetic.id.token.refreshed", "account_id": "fixture-account"}})
        fake.auth_refresh = refreshed
        report, fake = sp.run_clean_job(self.root, self.repo, fake_run=fake,
            tier={"cloud": "gpt-6-luna", "reasoning_effort": "low"}, escalates="J1")
        argv = fake.calls_of("exec")[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        self.assertNotIn("model_providers.local-omlx.base_url", " ".join(argv))
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("danger-full-access", argv)
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertEqual(report["escalates"], "J1")
        self.assertEqual(report["tier"], "cloud")
        self.assertFalse(report["cloud_auth"]["real_home_read"])
        self.assertEqual(report["cloud_auth"]["refresh_writeback"], "updated")
        source_auth = json.loads((Path(self.root) / "synthetic-codex-home" / "auth.json").read_text())
        self.assertEqual(source_auth["tokens"]["refresh_token"],
                         "synthetic-refresh-token-refreshed")
        saved_report = (Path(report["report_dir"]) / "report.json").read_text()
        self.assertNotIn("synthetic-access-token", saved_report)
        self.assertNotIn("synthetic-refresh-token", saved_report)
        self.assertIn("tests", report)
        self.assertIn("violations", report)

    def test_cloud_self_check_rejects_local_provider_pins(self):
        argv = lw.cloud_worker_argv("codex", Path("/tmp/worktree"), "model-from-catalog",
                                    "low", Path("/tmp/last-message"))
        self.assertEqual(lw.cloud_policy_check(argv, "model-from-catalog", "low"), [])
        argv.extend(["-c", 'model_providers.local-omlx.base_url="http://127.0.0.1:8000/v1"'])
        self.assertTrue(any("must not override local provider" in problem
                            for problem in lw.cloud_policy_check(argv, "model-from-catalog", "low")))

    def write_synthetic_auth(self, home, token="synthetic-access-token-1234"):
        home.mkdir(parents=True, exist_ok=True)
        path = home / "auth.json"
        path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": token, "refresh_token": "synthetic-refresh-token-1234",
            "id_token": "synthetic.id.token", "account_id": "fixture-account"}}))
        return path

    def test_auth_home_copies_only_auth_and_redacts_credential_values(self):
        source = Path(self.root) / "auth-source"
        source_file = self.write_synthetic_auth(source)
        report_dir = Path(self.root) / "report"
        job = report_dir / "codex-home"
        report_dir.mkdir(mode=0o700)
        job.mkdir(mode=0o700)
        secrets = []
        session = lw.CloudAuthSession(source, job, Path(self.root) / "state", secrets)
        self.assertEqual(sorted(path.name for path in job.iterdir()), ["auth.json"])
        self.assertEqual((job / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("synthetic-access-token-1234", secrets)
        self.assertEqual(session.finish()["refresh_writeback"], "unchanged")
        self.assertTrue(source_file.exists())

    def test_refreshed_auth_is_written_back_only_when_source_is_unchanged(self):
        source = Path(self.root) / "auth-source"
        source_file = self.write_synthetic_auth(source)
        report_dir = Path(self.root) / "report"
        job = report_dir / "codex-home"
        report_dir.mkdir(mode=0o700)
        job.mkdir(mode=0o700)
        session = lw.CloudAuthSession(source, job, Path(self.root) / "state", [])
        refreshed = json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "synthetic-new-access-token",
            "refresh_token": "synthetic-new-refresh-token"}})
        (job / "auth.json").write_text(refreshed)
        evidence = report_dir / "worker-attempt-1.jsonl"
        evidence.write_text('refreshed synthetic-new-access-token synthetic-new-refresh-token')
        result = session.finish()
        self.assertEqual(result["refresh_writeback"], "updated")
        self.assertEqual(result["retained_artifacts_scrubbed"], 1)
        self.assertNotIn("synthetic-new-access-token", evidence.read_text())
        self.assertNotIn("synthetic-new-refresh-token", evidence.read_text())
        self.assertEqual(json.loads(source_file.read_text())["tokens"]["refresh_token"],
                         "synthetic-new-refresh-token")

        report_dir2 = Path(self.root) / "report-2"
        job2 = report_dir2 / "codex-home"
        report_dir2.mkdir(mode=0o700)
        job2.mkdir(mode=0o700)
        session2 = lw.CloudAuthSession(source, job2, Path(self.root) / "state", [])
        source_file.write_text(json.dumps({"tokens": {"refresh_token": "external-change"}}))
        (job2 / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": "stale-refresh"}}))
        self.assertEqual(session2.finish()["refresh_writeback"], "concurrent-change-refused")
        self.assertEqual(json.loads(source_file.read_text())["tokens"]["refresh_token"],
                         "external-change")

    def test_active_job_lock_refuses_a_concurrent_cloud_auth_copy(self):
        source = Path(self.root) / "auth-source"
        self.write_synthetic_auth(source)
        job1 = Path(self.root) / "report-1" / "codex-home"
        job2 = Path(self.root) / "report-2" / "codex-home"
        job1.parent.mkdir(mode=0o700)
        job2.parent.mkdir(mode=0o700)
        job1.mkdir(mode=0o700)
        job2.mkdir(mode=0o700)
        session = lw.CloudAuthSession(source, job1, Path(self.root) / "state", [])
        with self.assertRaises(lw.BlockedError):
            lw.CloudAuthSession(source, job2, Path(self.root) / "state", [])
        session.finish()

    def test_symlinked_source_or_destination_auth_file_is_refused(self):
        source = Path(self.root) / "auth-source"
        source.mkdir()
        target = Path(self.root) / "target-auth.json"
        target.write_text('{"tokens": {"access_token": "synthetic-token"}}')
        (source / "auth.json").symlink_to(target)
        report_dir = Path(self.root) / "report"
        job = report_dir / "codex-home"
        report_dir.mkdir(mode=0o700)
        job.mkdir(mode=0o700)
        with self.assertRaises(lw.BlockedError):
            lw.CloudAuthSession(source, job, Path(self.root) / "state", [])

        source2 = Path(self.root) / "auth-source-2"
        self.write_synthetic_auth(source2)
        report_dir2 = Path(self.root) / "report-2"
        job2 = report_dir2 / "codex-home"
        report_dir2.mkdir(mode=0o700)
        job2.mkdir(mode=0o700)
        (job2 / "auth.json").symlink_to(target)
        with self.assertRaises(lw.BlockedError):
            lw.CloudAuthSession(source2, job2, Path(self.root) / "state", [])

    def test_cloud_model_listing_uses_the_live_codex_catalog(self):
        output = io.StringIO()
        completed = sp.fake_completed(stdout=sp.load_fixture_text("codex/debug_models.json"))
        with contextlib.redirect_stdout(output):
            result = lw.main(["models", "--cloud", "--json"], codex_run=lambda *a, **k: completed)
        catalog = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertIn("gpt-6-luna", [item["slug"] for item in catalog])

    def test_no_cloud_slug_is_hardcoded(self):
        for path in Path(lw.__file__).parent.glob("*.py"):
            self.assertNotRegex(path.read_text(), r"gpt-\d")


if __name__ == "__main__":
    unittest.main()
