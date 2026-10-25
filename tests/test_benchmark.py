"""Matched benchmark checks using real launcher-style evidence files."""
import hashlib
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import benchmark


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.criteria = ["result is correct", "semantic review accepted"]
        self.criteria_hash = hashlib.sha256(json.dumps(
            self.criteria, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)

    def arm_files(self, prefix, *, tier="cloud", input_tokens=100, cached=20,
                  output=10, attempts=1, report_status="ok", planner=None):
        task_id = "task-1"
        report = {
            "job_id": prefix + "-job", "task_id": task_id, "base_commit": "abc123",
            "tier": tier,
            "job_kind": "implementation", "status": report_status, "attempts": [
                {"attempt": i + 1, "returncode": 0 if report_status == "ok" else 1,
                 "usage": {"input_tokens": input_tokens // attempts,
                           "cached_input_tokens": cached // attempts,
                           "output_tokens": output // attempts,
                           "complete": True}}
                for i in range(attempts)],
            "tests": [{"status": "passed"}],
            "usage": {"input_tokens": input_tokens, "cached_input_tokens": cached,
                      "output_tokens": output, "complete": True},
            "elapsed_seconds": 12.5,
        }
        report_path = self.save(prefix + "-report.json", report)
        contract = {"task_id": task_id, "goal": "implement benchmark fixture",
                    "base_ref": "main", "acceptance_criteria": self.criteria,
                    "test_commands": [["python3", "-m", "unittest"]]}
        package_path, receipt_path = self.review_files(prefix, report["job_id"], action="accept",
                                                       report_path=report_path)
        summary = {"session": prefix, "ledger_integrity": "complete",
                   "jobs": [{"job_id": report["job_id"], "tier": tier,
                             "attempts": attempts, "status": report_status,
                             "tokens": {"input": input_tokens, "cached": cached,
                                        "output": output},
                             "durations": {"total": 12.5}}],
                   "totals": {tier: {"input_tokens": input_tokens,
                                     "cached_input_tokens": cached,
                                     "output_tokens": output}},
                   "planner": planner}
        summary_path = self.save(prefix + "-summary.json", summary)
        return {"summary": summary_path, "reports": [report_path],
                "accepted_job_id": report["job_id"],
                "elapsed_seconds": 50.0, "review_calls": 1,
                "package": package_path, "receipt": receipt_path,
                "review_records": []}

    def review_files(self, prefix, job_id, *, action, report_path=None):
        contract = {"task_id": "task-1", "goal": "implement benchmark fixture",
                    "base_ref": "main", "acceptance_criteria": self.criteria,
                    "test_commands": [["python3", "-m", "unittest"]]}
        bindings = {}
        if report_path:
            report_file = Path(report_path).resolve()
            bindings[str(report_file)] = hashlib.sha256(report_file.read_bytes()).hexdigest()
        package = {"job_id": job_id, "base_commit": "abc123", "complete": True,
                   "semantic_review_required": True, "contract": contract,
                   "kind": "write", "report_path": report_path,
                   "bindings": bindings, "checks": {"acceptance_checks": None}}
        package["package_sha256"] = benchmark.acceptance_spec_sha256(package)
        package_path = self.save(prefix + "-package.json", package)
        receipt_status = "accepted" if action == "accept" else "rejected"
        receipt = {"job_id": job_id, "status": receipt_status,
                   "decision": {"action": action, "semantic_review": True,
                                "reviewer": "cloud-planner", "package_sha256": package["package_sha256"],
                                "rationale": "Reviewed the full proposal and evidence.",
                                "reviewed_sections": ["diff", "sources", "tests", "findings",
                                                      "audit", "screenshots"]}}
        if action == "accept":
            receipt.update(applied=True,
                           integration_tests=[{"command": contract["test_commands"][0],
                                               "status": "passed"}],
                           archive={"status": "archived", "removed": True})
        receipt_path = self.save(prefix + "-receipt.json", receipt)
        return package_path, receipt_path

    def direct_arm(self, prefix, *, input_tokens=100, cached=20, output=10):
        planner = {"completeness": "final", "input_tokens": input_tokens,
                   "cached_input_tokens": cached, "output_tokens": output}
        summary = {"session": prefix, "ledger_integrity": "complete", "jobs": [],
                   "totals": {}, "planner": planner}
        summary_path = self.save(prefix + "-summary.json", summary)
        specification = self.specification()
        acceptance = {"task_id": "task-1", "session": prefix,
                      "specification_sha256": benchmark.acceptance_spec_sha256(specification),
                      "status": "accepted", "semantic_review": True,
                      "reviewer": "cloud-planner", "rationale": "The direct result passed.",
                      "metrics": {"attempts": 0, "failed_attempts": 0, "failed_jobs": 0,
                                  "rejected_jobs": 0, "semantic_rejections": 0,
                                  "repairs": 0, "repair_jobs": 0,
                                  "automatic_local_retries": 0, "review_calls": 1,
                                  "elapsed_seconds": 900.0}}
        acceptance_path = self.save(prefix + "-acceptance.json", acceptance)
        return {"summary": summary_path, "reports": [], "acceptance": acceptance_path,
                "elapsed_seconds": 900.0, "review_calls": 1, "repairs": 0}

    def specification(self):
        return {"goal": "implement benchmark fixture", "base_ref": "main",
                "base_commit": "abc123",
                "acceptance_criteria": self.criteria,
                "test_commands": [["python3", "-m", "unittest"]],
                "acceptance_checks_sha256": None}

    def manifest(self, baseline, launcher):
        specification = self.specification()
        return {"version": 1, "benchmark_id": "bench-1", "tasks": [{
            "task_id": "task-1",
            "specification": specification,
            "specification_sha256": benchmark.acceptance_spec_sha256(specification),
            "arms": {"direct_cloud": baseline, "launcher": launcher}}]}

    def test_matched_pair_counts_attempts_and_planner_without_double_count(self):
        baseline = self.direct_arm("base", input_tokens=100, cached=20, output=10)
        launcher = self.arm_files("launch", tier="local", input_tokens=5, cached=0,
                                  output=2, attempts=2,
                                  planner={"completeness": "final", "input_tokens": 30,
                                           "cached_input_tokens": 24, "output_tokens": 4})
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        result = benchmark.compare(path)
        pair = result["tasks"][0]
        self.assertTrue(pair["matched"])
        self.assertEqual(pair["arms"]["direct_cloud"]["cloud_tokens"],
                         {"cached_input": 20, "uncached_input": 80, "output": 10})
        self.assertEqual(pair["arms"]["launcher"]["cloud_tokens"],
                         {"cached_input": 24, "uncached_input": 6, "output": 4})
        self.assertEqual(pair["arms"]["launcher"]["attempts"], 2)
        self.assertEqual(pair["arms"]["launcher"]["rejected_jobs"], 0)

    def test_acceptance_identity_mismatch_disables_savings(self):
        baseline = self.direct_arm("base", input_tokens=20, cached=5, output=2)
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 1,
            "cached_input_tokens": 0, "output_tokens": 1})
        manifest = self.manifest(baseline, launcher)
        package = json.loads(Path(launcher["package"]).read_text())
        package["contract"]["goal"] = "a different goal"
        unsigned = dict(package)
        unsigned.pop("package_sha256")
        package["package_sha256"] = benchmark.acceptance_spec_sha256(unsigned)
        Path(launcher["package"]).write_text(json.dumps(package))
        receipt = json.loads(Path(launcher["receipt"]).read_text())
        receipt["decision"]["package_sha256"] = package["package_sha256"]
        Path(launcher["receipt"]).write_text(json.dumps(receipt))
        path = self.save("manifest.json", manifest)
        result = benchmark.compare(path)
        self.assertFalse(result["tasks"][0]["matched"])
        self.assertIsNone(result["tasks"][0]["savings_percent"])

    def test_missing_usage_and_incomplete_planner_remain_unknown(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "snapshot", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        report = json.loads(Path(launcher["reports"][0]).read_text())
        report["usage"]["output_tokens"] = None
        Path(launcher["reports"][0]).write_text(json.dumps(report))
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        result = benchmark.compare(path)
        arm = result["tasks"][0]["arms"]["launcher"]
        self.assertIsNone(arm["cloud_tokens"]["output"])
        self.assertFalse(result["tasks"][0]["matched"])
        self.assertIsNone(result["tasks"][0]["savings_percent"])

    def test_semantic_rejection_and_report_mismatch_block_pair(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        receipt = json.loads(Path(launcher["receipt"]).read_text())
        receipt["status"] = "rejected"
        Path(launcher["receipt"]).write_text(json.dumps(receipt))
        report = json.loads(Path(launcher["reports"][0]).read_text())
        report["task_id"] = "other-task"
        Path(launcher["reports"][0]).write_text(json.dumps(report))
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        pair = benchmark.compare(path)["tasks"][0]
        self.assertFalse(pair["matched"])
        self.assertIsNone(pair["savings_percent"])

    def test_zero_baseline_denominator_has_no_percentage(self):
        baseline = self.direct_arm("base", input_tokens=0, cached=0, output=0)
        launcher = self.arm_files("launch", tier="local", input_tokens=0, cached=0,
                                  output=0, planner={"completeness": "final",
                                  "input_tokens": 0, "cached_input_tokens": 0,
                                  "output_tokens": 0})
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        pair = benchmark.compare(path)["tasks"][0]
        self.assertTrue(pair["matched"])
        self.assertIsNone(pair["savings_percent"])

    def test_optional_direct_luna_arm_needs_no_model_name(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        luna = self.direct_arm("luna", input_tokens=60, cached=30, output=8)
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        manifest = json.loads(Path(path).read_text())
        manifest["tasks"][0]["arms"]["direct_luna"] = luna
        Path(path).write_text(json.dumps(manifest))
        result = benchmark.compare(path)
        self.assertTrue(result["tasks"][0]["matched"])
        self.assertIn("direct_luna", result["tasks"][0]["arms"])

    def test_direct_cloud_can_use_final_planner_rollout_without_worker_jobs(self):
        baseline = self.direct_arm("base", input_tokens=100, cached=25, output=8)
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 20,
            "cached_input_tokens": 5, "output_tokens": 2})
        manifest = self.manifest(baseline, launcher)
        path = self.save("manifest.json", manifest)
        pair = benchmark.compare(path)["tasks"][0]
        self.assertTrue(pair["matched"])
        self.assertEqual(pair["arms"]["direct_cloud"]["cloud_tokens"],
                         {"cached_input": 25, "uncached_input": 75, "output": 8})

    def test_rejected_worker_job_and_failed_attempt_remain_counted(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="local", attempts=2, planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        summary_path = Path(launcher["summary"])
        summary = json.loads(summary_path.read_text())
        failed = {"job_id": "launch-rejected", "task_id": "task-1", "base_commit": "abc123",
                  "tier": "local",
                  "job_kind": "repair", "status": "worker_failed", "elapsed_seconds": 3.0,
                  "acceptance_checks": {"status": "failed"},
                  "tests": [{"status": "failed"}],
                  "attempts": [{"attempt": 1, "returncode": 0,
                                "tests": [{"status": "failed"}],
                                "acceptance_checks": {"status": "failed"},
                                "usage": {"input_tokens": 7, "cached_input_tokens": 0,
                                          "output_tokens": 2, "complete": True}}],
                  "usage": {"input_tokens": 7, "cached_input_tokens": 0,
                            "output_tokens": 2, "complete": True}}
        failed_path = self.save("launch-rejected.json", failed)
        reject_package, reject_receipt = self.review_files("reject", failed["job_id"],
                                                            action="reject", report_path=failed_path)
        launcher["reports"].append(failed_path)
        launcher["review_records"] = [{"job_id": failed["job_id"],
                                      "package": reject_package,
                                      "receipt": reject_receipt}]
        launcher["review_calls"] = 2
        summary["jobs"].append({"job_id": failed["job_id"], "tier": "local", "attempts": 1,
                                "status": "worker_failed",
                                "tokens": {"input": 7, "cached": 0, "output": 2}})
        summary["totals"]["local"] = {"input_tokens": 12, "cached_input_tokens": 0,
                                       "output_tokens": 4}
        summary_path.write_text(json.dumps(summary))
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        arm = benchmark.compare(path)["tasks"][0]["arms"]["launcher"]
        self.assertEqual(arm["attempts"], 3)
        self.assertEqual(arm["failed_attempts"], 1)
        self.assertEqual(arm["failed_jobs"], 1)
        self.assertEqual(arm["rejected_jobs"], 1)
        self.assertEqual(arm["semantic_rejections"], 1)
        self.assertEqual(arm["repair_jobs"], 1)
        self.assertEqual(arm["automatic_local_retries"], 1)
        self.assertEqual(arm["repairs"], 2)

    def test_acceptance_like_receipt_without_integration_or_archive_cannot_claim_savings(self):
        mutations = (
            lambda receipt: receipt.update(applied=False),
            lambda receipt: receipt.update(integration_tests=[]),
            lambda receipt: receipt.update(integration_tests=[{"command": ["other"],
                                                                "status": "passed"}]),
            lambda receipt: receipt.update(archive={"status": "archived", "removed": False}),
        )
        for index, mutate in enumerate(mutations):
            baseline = self.direct_arm("base-%d" % index)
            launcher = self.arm_files("launch-%d" % index, tier="local", planner={
                "completeness": "final", "input_tokens": 30,
                "cached_input_tokens": 24, "output_tokens": 4})
            receipt = json.loads(Path(launcher["receipt"]).read_text())
            mutate(receipt)
            Path(launcher["receipt"]).write_text(json.dumps(receipt))
            path = self.save("manifest-%d.json" % index,
                             self.manifest(baseline, launcher))
            pair = benchmark.compare(path)["tasks"][0]
            self.assertFalse(pair["matched"])
            self.assertIsNone(pair["savings_percent"])

    def test_accepted_write_requires_each_pinned_check_in_integration_receipt(self):
        launcher = self.arm_files("launch-trusted-check", tier="local", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        script = self.root / "trusted-check.py"
        script.write_text("assert True\n")
        (self.root / "nested").mkdir()
        raw_script = str(self.root / "nested" / ".." / script.name)
        manifest_path = self.root / "trusted-checks.json"
        check = {"name": "semantic-contract", "argv": ["python3", raw_script, raw_script],
                 "scripts": [raw_script]}
        manifest_path.write_text(json.dumps({"version": 1, "checks": [check]}))
        package = json.loads(Path(launcher["package"]).read_text())
        package["contract"]["acceptance_checks"] = str(manifest_path)
        package["bindings"][str(manifest_path.resolve())] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        package["bindings"][str(script.resolve())] = hashlib.sha256(script.read_bytes()).hexdigest()
        package.pop("package_sha256")
        package["package_sha256"] = benchmark.acceptance_spec_sha256(package)
        Path(launcher["package"]).write_text(json.dumps(package))
        receipt = json.loads(Path(launcher["receipt"]).read_text())
        receipt["decision"]["package_sha256"] = package["package_sha256"]
        receipt["integration_tests"].append({
            "name": check["name"], "command": ["python3", str(script.resolve()), raw_script],
            "status": "passed"})
        Path(launcher["receipt"]).write_text(json.dumps(receipt))
        report = Path(launcher["reports"][0]).resolve()
        specification = self.specification()
        specification["acceptance_checks_sha256"] = benchmark._package_checks_sha256(package)
        valid, issues = benchmark._review_record(
            {"package": launcher["package"], "receipt": launcher["receipt"]},
            "task-1", specification, "launch-trusted-check-job", "accept", str(report))
        self.assertTrue(valid, issues)

        receipt["integration_tests"] = [item for item in receipt["integration_tests"]
                                        if item.get("name") != check["name"]]
        Path(launcher["receipt"]).write_text(json.dumps(receipt))
        valid, issues = benchmark._review_record(
            {"package": launcher["package"], "receipt": launcher["receipt"]},
            "task-1", specification, "launch-trusted-check-job", "accept", str(report))
        self.assertFalse(valid)
        self.assertTrue(any("trusted acceptance" in issue for issue in issues))

    def test_direct_arms_refuse_worker_reports_to_prevent_omitted_planner_cost(self):
        direct = self.direct_arm("direct")
        direct["reports"] = self.arm_files("unaccounted-cloud-worker")["reports"]
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        path = self.save("manifest.json", self.manifest(direct, launcher))
        pair = benchmark.compare(path)["tasks"][0]
        self.assertFalse(pair["matched"])
        self.assertIsNone(pair["savings_percent"])
        self.assertTrue(any("direct comparison arms" in issue
                            for issue in pair["issues"]["direct_cloud"]))

    def test_overlapping_planner_child_and_worker_thread_withholds_cloud_totals(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="cloud", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4,
            "subagents": {"count": 1, "sessions": ["same-thread"]}})
        report_path = Path(launcher["reports"][0])
        report = json.loads(report_path.read_text())
        report["attempts"][0]["thread_id"] = "same-thread"
        report_path.write_text(json.dumps(report))
        package = json.loads(Path(launcher["package"]).read_text())
        raw_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        package["bindings"][str(report_path.resolve())] = raw_hash
        unsigned = dict(package)
        unsigned.pop("package_sha256")
        package["package_sha256"] = benchmark.acceptance_spec_sha256(unsigned)
        Path(launcher["package"]).write_text(json.dumps(package))
        receipt = json.loads(Path(launcher["receipt"]).read_text())
        receipt["decision"]["package_sha256"] = package["package_sha256"]
        Path(launcher["receipt"]).write_text(json.dumps(receipt))
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        pair = benchmark.compare(path)["tasks"][0]
        self.assertFalse(pair["matched"])
        self.assertIsNone(pair["arms"]["launcher"]["cloud_tokens"]["output"])
        self.assertTrue(any("overlap" in issue for issue in pair["issues"]["launcher"]))

    def test_cli_writes_json_for_a_matched_manifest(self):
        baseline = self.direct_arm("base")
        launcher = self.arm_files("launch", tier="local", planner={
            "completeness": "final", "input_tokens": 30,
            "cached_input_tokens": 24, "output_tokens": 4})
        path = self.save("manifest.json", self.manifest(baseline, launcher))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = benchmark.main(["--manifest", path])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["all_tasks_matched"])

    def test_example_frozen_spec_fingerprint_is_valid(self):
        example = json.loads((Path(__file__).resolve().parents[1] / "examples" /
                              "benchmark.json").read_text())
        task = example["tasks"][0]
        self.assertEqual(task["specification_sha256"],
                         benchmark.acceptance_spec_sha256(task["specification"]))

    def test_trusted_check_identity_ignores_temp_paths_but_binds_script_content(self):
        packages = []
        for directory in (self.root / "copy-a", self.root / "copy-b"):
            directory.mkdir()
            script = directory / "check.py"
            script.write_text("assert 2 + 2 == 4\n")
            manifest_path = directory / "checks.json"
            manifest_path.write_text(json.dumps({"version": 1, "checks": [{
                "name": "arithmetic", "argv": ["python3", str(script)],
                "scripts": [str(script)]}]}))
            bindings = {str(script.resolve()): hashlib.sha256(script.read_bytes()).hexdigest(),
                        str(manifest_path.resolve()): hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
            packages.append({"contract": {"acceptance_checks": str(manifest_path)},
                             "bindings": bindings})
        self.assertEqual(benchmark._package_checks_sha256(packages[0]),
                         benchmark._package_checks_sha256(packages[1]))
        changed_script = Path(packages[1]["contract"]["acceptance_checks"]).parent / "check.py"
        changed_script.write_text("assert 2 + 2 == 5\n")
        with self.assertRaisesRegex(benchmark.BenchmarkError, "changed"):
            benchmark._package_checks_sha256(packages[1])


if __name__ == "__main__":
    unittest.main()
