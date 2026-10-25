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
        fake.worker_stderr = "synthetic-access-token-1234 synthetic-refresh-token-1234 " \
                             "synthetic-refresh-token-refreshed"
        refreshed = json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "synthetic-access-token-refreshed",
            "refresh_token": "synthetic-refresh-token-refreshed",
            "id_token": "synthetic.id.token.refreshed", "account_id": "fixture-account"}})
        fake.auth_refresh = refreshed
        source_home = Path(self.root) / "synthetic-codex-home"
        original_auth = self.write_synthetic_auth(source_home).read_bytes()
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
        self.assertEqual(fake.calls_of("exec")[0]["env"]["CODEX_HOME"], str(source_home))
        self.assertEqual(report["cloud_auth"]["source_home"], str(source_home))
        self.assertEqual(report["cloud_auth"]["credential_scrub"], "observed after Codex exit")
        source_bytes = (source_home / "auth.json").read_bytes()
        self.assertEqual(source_bytes, refreshed.encode("utf-8"))
        self.assertNotEqual(source_bytes, original_auth)
        profile = (Path(report["report_dir"]) / "permissions.sb").read_text()
        source_real = source_home.resolve()
        self.assertIn(f'(subpath "{source_real}")', profile)
        self.assertIn(f'(allow file-read* (literal "{source_real}/auth.json") '
                      f'(subpath "{source_real}/tmp/arg0-macos"))', profile)
        self.assertIn(f'(allow file-write* (literal "{source_real}/auth.json")', profile)
        self.assertNotIn(f'(allow file-read* (subpath "{source_real}")', profile)
        saved_report = (Path(report["report_dir"]) / "report.json").read_text()
        self.assertNotIn("synthetic-access-token", saved_report)
        self.assertNotIn("synthetic-refresh-token", saved_report)
        stderr_log = (Path(report["report_dir"]) / "worker-attempt-1.stderr.log").read_text()
        self.assertNotIn("synthetic-access-token-1234", stderr_log)
        self.assertNotIn("synthetic-refresh-token-1234", stderr_log)
        self.assertNotIn("synthetic-refresh-token-refreshed", stderr_log)
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

    def test_cloud_home_is_preserved_when_codex_does_not_refresh(self):
        source_home = Path(self.root) / "synthetic-codex-home"
        auth_path = self.write_synthetic_auth(source_home)
        original = auth_path.read_bytes()
        report, fake = sp.run_clean_job(self.root, self.repo, fake_run=sp.FakeRun(
            worker_edit=sp.fix_add), tier={"cloud": "gpt-6-luna", "reasoning_effort": "low"})
        self.assertEqual(fake.calls_of("exec")[0]["env"]["CODEX_HOME"], str(source_home))
        self.assertEqual(auth_path.read_bytes(), original)
        self.assertFalse((Path(report["report_dir"]) / "codex-home").exists())

    def test_cloud_home_selector_rejects_symlinked_auth(self):
        source = Path(self.root) / "auth-source"
        source.mkdir()
        target = Path(self.root) / "target-auth.json"
        target.write_text('{"tokens": {"access_token": "synthetic-token"}}')
        (source / "auth.json").symlink_to(target)
        with self.assertRaises(lw.BlockedError):
            lw.resolve_cloud_codex_home({"CODEX_HOME": str(source)})

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
