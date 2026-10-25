import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ledger
import local_worker


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.home = self.root / "codex"

    def tearDown(self):
        self.temp.cleanup()

    def job(self, job_id, *, tier="local", model="local-test", tokens=None,
            status="ok", escalates=None):
        counts = None if tokens is None else dict(zip(
            ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"),
            tokens))
        return {
            "job_id": job_id, "kind": "write", "tier": tier, "model": model,
            "model_profile": {"model": {"canonical_id": model}, "context_window": 65536},
            "attempts": [{"attempt": 1}], "timing": {"worker_seconds": 1.5,
                "verification_seconds": 2.0, "native_preflight_seconds": 0.0},
            "elapsed_seconds": 4.0, "token_counts": counts, "usage": {
                "reasoning_output_tokens": None if counts is None else counts["reasoning_tokens"]},
            "status": status, "classification": {"code": "tests_failed" if status != "ok" else "ok",
                "plugin_suggestion": "Suggested fixes for this plugin" if status != "ok" else None},
            "escalates": escalates, "report_path": "/reports/" + job_id + "/report.json",
            "screenshots": {"index": "/screenshots/index.md"},
        }

    def planner_home(self, *, session):
        path = self.home / "sessions" / "2026" / "rollout_planner.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "payload": {"id": session, "session_id": session}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 8,
                    "output_tokens": 3, "reasoning_output_tokens": 2, "total_tokens": 13}}}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return self.home

    def test_summary_totals_local_cloud_and_planner_tokens(self):
        for report in (self.job("J1", tokens=(47610, 39169, 627, 190), status="tests_failed"),
                       self.job("J2", tier="cloud", model="gpt-6-luna",
                                tokens=(12000, 0, 900, 0), escalates="J1")):
            ledger.append_job(self.state, "S1", report)
        codex_home = self.planner_home(session="S1")
        summary = ledger.build_summary(self.state, "S1", codex_home)
        self.assertEqual(summary["totals"]["local"]["input_tokens"], 47610)
        self.assertEqual(summary["totals"]["cloud"]["output_tokens"], 900)
        self.assertIsNotNone(summary["planner"])
        self.assertEqual(summary["escalations"], [{"from": "J1", "to": "J2", "reason": "tests_failed"}])
        md = ledger.render_markdown(summary)
        self.assertIn("Suggested fixes for this plugin", md)
        self.assertIn("J2", md)
        self.assertIn("Unlinked subagent usage may be absent", md)

    def test_missing_data_is_shown_as_missing(self):
        ledger.append_job(self.state, "S2", self.job("J3", tokens=None))
        summary = ledger.build_summary(self.state, "S2", self.home)
        self.assertIn("not reported", ledger.render_markdown(summary))
        self.assertNotIn("| 0 |", ledger.render_markdown(summary))

    def test_planner_usage_only_reads_matching_session_token_counts(self):
        home = self.planner_home(session="another-session")
        match = home / "sessions" / "2026" / "rollout_match.jsonl"
        match.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": "S3"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 25, "output_tokens": 4,
                    "cached_input_tokens": 20, "reasoning_output_tokens": 1}}}}),
        ]) + "\n")
        result = ledger.planner_usage("S3", home)
        self.assertEqual(result["input_tokens"], 25)
        self.assertEqual(result["output_tokens"], 4)
        self.assertEqual(result["subagents"], "not linked")

    def test_linked_child_usage_is_included_and_missing_fields_stay_missing(self):
        child = self.home / "sessions" / "2026" / "rollout_child.jsonl"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": "child-1", "parent_session_id": "S4"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 5, "output_tokens": 2}}}}),
        ]) + "\n")
        parent = self.planner_home(session="S4")
        result = ledger.planner_usage("S4", parent)
        self.assertEqual(result["subagents"]["count"], 1)
        self.assertIsNone(result["cached_input_tokens"])
        self.assertIsNone(result["reasoning_tokens"])

    def test_corrupt_ledger_marks_summary_partial_and_withholds_totals(self):
        path = ledger.append_job(self.state, "S5", self.job("J5", tokens=(10, 2, 3, 1)))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{truncated")
        summary = ledger.build_summary(self.state, "S5", self.home)
        self.assertEqual(summary["ledger_integrity"], "partial")
        self.assertIsNone(summary["all_job_totals"]["input_tokens"])

    def test_known_token_values_do_not_hide_a_missing_job_value(self):
        ledger.append_job(self.state, "S6", self.job("J6", tokens=(10, 5, 2, 1)))
        ledger.append_job(self.state, "S6", self.job("J7", tokens=None))
        summary = ledger.build_summary(self.state, "S6", self.home)
        self.assertIsNone(summary["all_job_totals"]["input_tokens"])

    def test_local_share_is_missing_when_a_required_input_or_output_is_missing(self):
        ledger.append_job(self.state, "S9", self.job("J9", tokens=(10, 5, None, 1)))
        summary = ledger.build_summary(self.state, "S9", self.home)
        self.assertIsNone(summary["local_share"])

    def test_session_id_falls_back_to_thread_then_utc_manual_date(self):
        self.assertEqual(ledger.session_id("chosen"), "chosen")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-123"}):
            self.assertEqual(ledger.session_id(), "thread-123")
        with mock.patch.dict(os.environ):
            os.environ.pop("CODEX_THREAD_ID", None)
            self.assertRegex(ledger.session_id(), r"^manual-\d{4}-\d{2}-\d{2}$")

    def test_summary_command_writes_both_formats(self):
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(self.home),
                                             "LOCAL_WORKER_NOTIFY": "0"}):
            code = local_worker.main(["summary", "--session", "S8",
                                      "--state-dir", str(self.state)])
        self.assertEqual(code, 0)
        self.assertTrue((self.state / "sessions" / "S8" / "summary.md").is_file())
        self.assertTrue((self.state / "sessions" / "S8" / "summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
