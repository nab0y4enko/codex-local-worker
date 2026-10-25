"""Test-quality diagnostics reach the planner without changing verification status."""
import dataclasses
import json
import unittest
from pathlib import Path
from unittest import mock

import support as sp

lw = sp.lw


class TestReviewIntegration(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root / "repo")

    def test_missing_assertions_are_visible_in_report_and_planner_json(self):
        options = sp.job_options(self.root, self.repo)
        options = dataclasses.replace(options, task=dataclasses.replace(
            options.task, allowed_paths=["add.py", "tests/test_add.py"]))

        def edit(cwd, attempt=1):
            sp.fix_add(cwd, attempt)
            path = Path(cwd) / "tests/test_add.py"
            path.parent.mkdir(exist_ok=True)
            path.write_text("def test_sum():\n    result = 2 + 3\n    expected = 5\n")

        with mock.patch.object(lw, "run_bounded", sp.FakeRun(worker_edit=edit)):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "ok")
        findings = report.get("review_findings")
        self.assertTrue(findings, "passing tests must not hide missing assertion diagnostics")
        self.assertEqual(findings[0]["code"], "missing_assertion")
        self.assertEqual(findings[0]["path"], "tests/test_add.py")
        self.assertEqual(lw.planner_json(report)["review_findings"], findings)
        saved = json.loads(Path(report["report_dir"], "report.json").read_text())
        self.assertEqual(saved["review_findings"], findings)

    def test_worker_prompt_requires_assertions_and_quality_self_check(self):
        task = sp.job_options(self.root, self.repo).task
        prompt = lw.build_prompt(task, self.repo)
        self.assertIn("assert observable behavior", prompt)
        self.assertIn("test_review.py", prompt)
        self.assertIn("advisory", prompt)

    def test_unperformed_review_is_unavailable_instead_of_zero_findings(self):
        report = lw.new_report(sp.job_options(self.root, self.repo))
        self.assertIsNone(report.get("review_findings"))
        self.assertIsNone(lw.planner_json(report)["review_findings"])

    def test_advisory_failure_cannot_change_the_job_outcome(self):
        options = sp.job_options(self.root, self.repo)
        with mock.patch.object(lw, "run_bounded", sp.FakeRun(worker_edit=sp.fix_add)), \
                mock.patch.object(lw.test_review, "review_python_tests",
                                  side_effect=RuntimeError("private source text")):
            report = lw.run_job(options)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["review_findings"][0]["code"], "review_unavailable")
        self.assertNotIn("private source text", json.dumps(report["review_findings"]))
