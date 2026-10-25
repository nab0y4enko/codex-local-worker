"""Contract v2 parsing, compatibility and environment handling."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support as sp
import contract
import local_worker as lw


class ContractV2Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="contract-v2-"))
        self.repo = sp.make_repo(self.root / "repo")

    def tearDown(self):
        sp.cleanup(self.root)

    def v1_payload(self):
        return sp.task_payload(self.repo)

    def write_payload(self, **overrides):
        payload = self.v1_payload()
        payload.update({"kind": "write", "workstream": "task-1",
                        "environment": {"pass": []}, "tier": {"local": True},
                        "ui_review": False})
        payload.update(overrides)
        return payload

    def read_payload(self):
        payload = {"task_id": "read-1", "kind": "read", "repo_path": str(self.repo),
                   "base_ref": "main", "goal": "Inspect source", "timeout_seconds": 60,
                   "workstream": "read-1", "environment": {"pass": []},
                   "tier": {"local": True}, "question": "What does add do?",
                   "scope_paths": ["add.py"], "answer_schema": {"type": "string"},
                   "max_answer_bytes": 4096}
        return payload

    def test_v1_contract_upgrades_to_write(self):
        c = contract.Contract.from_payload(self.v1_payload())
        self.assertEqual((c.kind, c.workstream, c.tier.kind), ("write", "task-1", "local"))
        self.assertEqual(lw.Task, contract.Contract)

    def test_read_contract_requires_question_and_schema(self):
        c = contract.Contract.from_payload(self.read_payload())
        self.assertEqual(c.kind, "read")
        self.assertEqual(c.allowed_paths, [])
        with self.assertRaises(lw.ContractError):
            contract.Contract.from_payload({**self.read_payload(), "answer_schema": {
                "type": "object", "patternProperties": {}}})

    def test_read_contract_requires_a_source_scope_for_mandatory_citations(self):
        with self.assertRaisesRegex(lw.ContractError, "scope_paths"):
            contract.Contract.from_payload({**self.read_payload(), "scope_paths": []})

    def test_read_example_is_accepted_by_the_contract_parser(self):
        example_path = Path(__file__).resolve().parents[1] / "examples" / "read-task.json"
        payload = json.loads(example_path.read_text(encoding="utf-8"))
        payload["repo_path"] = str(self.repo)
        parsed = contract.Contract.from_payload(payload)
        self.assertEqual(parsed.kind, "read")
        self.assertEqual(parsed.goal, "Inspect existing repository conventions")
        self.assertEqual(parsed.scope_paths, ["src", "tests"])

    def test_environment_pass_refuses_routing_names_and_redacts_values(self):
        with self.assertRaises(lw.ContractError):
            contract.Contract.from_payload(self.write_payload(environment={"pass": ["OPENAI_API_KEY"]}))
        value = "/opt/jdk-secretish"
        with mock.patch.dict(os.environ, {"JAVA_HOME": value}):
            settings = sp.write_settings(self.root / "omlx" / "settings.json")
            options = lw.JobOptions(
                task=contract.Contract.from_payload(self.write_payload(
                    environment={"pass": ["JAVA_HOME"]})),
                state_dir=self.root / "state", codex_bin="codex-mock",
                settings_path=settings, omlx_fetch=sp.FakeOmlx("one"),
                verify=True, codex_run=sp.fake_help())
            fake = sp.FakeRun(worker_edit=sp.fix_add)
            with mock.patch.object(lw, "run_bounded", fake):
                report = lw.run_job(options)
        self.assertEqual(fake.verification_calls()[0]["env"]["JAVA_HOME"], value)
        saved = Path(report["report_dir"], "report.json").read_text(encoding="utf-8")
        self.assertNotIn(value, saved)
        self.assertNotIn(value, json.dumps(report))

    def test_cloud_tier_needs_a_model(self):
        with self.assertRaises(lw.ContractError):
            contract.Contract.from_payload({**self.write_payload(), "tier": {"cloud": ""}})


if __name__ == "__main__":
    unittest.main()
