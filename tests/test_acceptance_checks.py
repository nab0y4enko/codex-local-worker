"""Trusted planner-supplied acceptance check manifests."""
import json
import tempfile
import sys
from unittest import mock
import unittest
from pathlib import Path

import support as sp
import acceptance_checks
import contract


class AcceptanceCheckManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="acceptance-checks-"))
        self.repo = sp.make_repo(self.root / "repo")
        self.script = self.root / "check.py"
        self.script.write_text("print('ok')\n", encoding="utf-8")
        self.manifest = self.root / "checks.json"
        self.write_manifest()

    def tearDown(self):
        sp.cleanup(self.root)

    def write_manifest(self, **overrides):
        payload = {"version": 1, "checks": [{"name": "unit", "argv": ["python3", str(self.script)],
                                              "scripts": [str(self.script)]}]}
        payload.update(overrides)
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def test_manifest_is_snapshotted_and_scripts_are_protected_inputs(self):
        snapshot = acceptance_checks.load_manifest(self.manifest, repo=self.repo,
                                                    writable_roots=[self.root / "worker"])
        self.assertEqual(snapshot.protected_paths, [self.manifest.resolve(), self.script.resolve()])
        self.assertEqual(snapshot.checks[0]["name"], "unit")
        self.assertTrue(acceptance_checks.verify_snapshot(snapshot).ok)
        pins = [{"path": str(pin.path), "sha256": pin.sha256,
                 "device": pin.device, "inode": pin.inode} for pin in snapshot.pins]
        self.assertTrue(acceptance_checks.matches_reported_pins(snapshot, pins))
        self.assertFalse(acceptance_checks.matches_reported_pins(snapshot, []))

    def test_manifest_tampering_is_detected(self):
        snapshot = acceptance_checks.load_manifest(self.manifest, repo=self.repo)
        self.script.write_text("print('changed')\n", encoding="utf-8")
        result = acceptance_checks.verify_snapshot(snapshot)
        self.assertFalse(result.ok)
        self.assertIn(str(self.script.resolve()), result.changed_paths)

    def test_script_inside_worker_checkout_is_rejected(self):
        script = self.repo / "check.py"
        script.write_text("pass\n", encoding="utf-8")
        self.write_manifest(checks=[{"name": "unsafe", "argv": ["python3", str(script)],
                                    "scripts": [str(script)]}])
        with self.assertRaisesRegex(acceptance_checks.AcceptanceCheckError, "worker-writable"):
            acceptance_checks.load_manifest(self.manifest, repo=self.repo)

    def test_check_failure_is_reported_separately_and_uses_bounded_verifier(self):
        self.write_manifest(checks=[{"name": "reject", "argv": ["python3", "-c", "raise SystemExit(9)"],
                                    "scripts": []}])
        snapshot = acceptance_checks.load_manifest(self.manifest, repo=self.repo)
        task = contract.Contract.from_payload(sp.task_payload(self.repo))
        report = {"acceptance_checks": {"status": "pending", "checks": []}}
        wrapper = type("AppliedWrapper", (), {"applied": True,
                         "wrap": lambda self, argv: list(argv)})()
        outcomes = sp.lw.run_acceptance_checks(
            task, snapshot, report, self.repo, {}, "codex", {"available": True}, [],
            sp.lw.Deadline(30), wrapper)
        self.assertEqual(outcomes[0]["status"], "failed")
        self.assertEqual(outcomes[0]["returncode"], 9)
        self.assertEqual(report["acceptance_checks"]["status"], "failed")

    def test_check_is_blocked_when_deny_wrapper_is_unavailable(self):
        snapshot = acceptance_checks.load_manifest(self.manifest, repo=self.repo)
        task = contract.Contract.from_payload(sp.task_payload(self.repo))
        report = {"acceptance_checks": {"status": "pending", "checks": []}}
        outcomes = sp.lw.run_acceptance_checks(
            task, snapshot, report, self.repo, {}, "codex", {"available": True}, [],
            sp.lw.Deadline(30), None)
        self.assertEqual(outcomes[0]["status"], "blocked")
        self.assertIn("deny wrapper", outcomes[0]["reason"])
        feedback = sp.lw.summarize_failure({}, outcomes)
        self.assertIn("trusted acceptance checks require an applied deny wrapper", feedback)

    def test_real_check_drives_bounded_worker_repair_and_final_report(self):
        self.script.write_text("import os, sys\nsys.path.insert(0, os.getcwd())\nfrom add import add\nassert add(2, 3) == 5, 'addition must return five'\n")
        self.write_manifest(checks=[{'name': 'addition',
            'argv': [sys.executable, str(self.script.resolve())],
            'scripts': [str(self.script.resolve())]}])
        options = sp.job_options(self.root, self.repo, max_attempts=2, kind='write',
            acceptance_checks=str(self.manifest.resolve()),
            test_commands=[[sys.executable, '-c', 'pass']])
        def edit(cwd, attempt):
            (cwd / 'add.py').write_text(sp.FIX_ADD if attempt == 2 else
                                       'def add(a, b):\n    return 0\n')
        fake = sp.FakeRun(worker_edit=edit)
        real = sp.lw.run_bounded
        def hybrid(argv, **kwargs):
            inner, wrapped = sp.deny_wrapped([str(a) for a in argv])
            if inner[1:2] == ['exec']:
                return fake(argv, **kwargs)
            return real(argv, **kwargs)
        with mock.patch.object(sp.lw, 'run_bounded', hybrid):
            report = sp.lw.run_job(options)
        self.assertEqual(report['status'], 'ok', report.get('failure_reason'))
        self.assertEqual(len(report['attempts']), 2)
        self.assertEqual(report['attempts'][0]['tests'][-1]['status'], 'failed')
        self.assertEqual(report['attempts'][1]['tests'][-1]['status'], 'passed')
        prompt = Path(report['report_dir'], 'attempt-2.prompt.txt').read_text()
        self.assertIn('addition must return five', prompt)
        self.assertEqual(report['acceptance_checks']['status'], 'passed')
        self.assertTrue(report['permissions']['deny_list_applied'])
        self.assertEqual(report['contract']['acceptance_checks'], str(self.manifest.resolve()))
        saved = json.loads(Path(report['report_dir'], 'report.json').read_text())
        self.assertEqual(saved['acceptance_checks']['status'], 'passed')
        self.assertLess(report['budget']['elapsed_seconds'], options.task.timeout_seconds)

    def test_no_verify_cannot_bypass_independent_checks(self):
        options = sp.job_options(self.root, self.repo, verify=False, kind='write',
                                acceptance_checks=str(self.manifest.resolve()))
        fake = sp.FakeRun(worker_edit=sp.fix_add)
        with mock.patch.object(sp.lw, 'run_bounded', fake):
            report = sp.lw.run_job(options)
        self.assertNotEqual(report['status'], 'ok')
        self.assertEqual(fake.exec_calls, 0)
        self.assertIn('--no-verify', report['failure_reason'])

    def test_relative_and_symlink_manifest_contracts_are_rejected(self):
        alias = self.root / "alias.json"
        alias.symlink_to(self.manifest)
        for supplied in ("checks.json", str(alias)):
            payload = dict(sp.task_payload(self.repo), kind="write", acceptance_checks=supplied)
            with self.subTest(path=supplied), self.assertRaises(contract.ContractError):
                contract.Contract.from_payload(payload)

    def test_contract_acceptance_checks_are_opt_in_and_round_trip(self):
        payload = sp.task_payload(self.repo)
        legacy = contract.Contract.from_payload(payload)
        self.assertIsNone(legacy.acceptance_checks)
        payload.update({"kind": "write", "workstream": "task-1", "environment": {"pass": []},
                        "tier": {"local": True}, "ui_review": False,
                        "acceptance_checks": str(self.manifest)})
        parsed = contract.Contract.from_payload(payload)
        self.assertEqual(parsed.acceptance_checks, self.manifest.resolve())
        self.assertEqual(parsed.to_dict()["acceptance_checks"], str(self.manifest.resolve()))


if __name__ == "__main__":
    unittest.main()
