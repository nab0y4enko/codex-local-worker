"""Opt-in end-to-end qualification against the installed Local Worker and live services.

Run with LOCAL_WORKER_LIVE=1. All jobs use the real bin/local-worker entry point; local
inference is never replaced by a direct Codex invocation. Cloud inference and native simulator
qualification are separately gated by explicit environment variables.
"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "bin" / "local-worker"
PREPARE_READ = ROOT / "scripts" / "prepare_read_pilot.py"
PREPARE_WRITE = ROOT / "scripts" / "prepare_pilot.py"
LIVE = os.environ.get("LOCAL_WORKER_LIVE") == "1"


def _json_command(argv, *, timeout=120, cwd=ROOT, env=None):
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    completed = subprocess.run([str(item) for item in argv], cwd=str(cwd), env=process_env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=timeout, check=False)
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        payload = None
    return completed, payload


@unittest.skipUnless(LIVE, "set LOCAL_WORKER_LIVE=1 to run live qualification")
class LiveQualification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        requested = os.environ.get("LOCAL_WORKER_QUALIFICATION_ROOT")
        cls.root = Path(requested).expanduser().resolve() if requested else Path(
            tempfile.mkdtemp(prefix="local-worker-task26-"))
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.state = cls.root / "state"
        cls.state.mkdir(exist_ok=True)
        cls.session = "task26-" + _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        cls.observations = []
        cls.reports = []
        cls.current_started = None
        cls.current_name = None
        cls.current_metrics = {}
        cls.native_plan = os.environ.get("LOCAL_WORKER_LIVE_NATIVE_PLAN")
        cls.cloud_enabled = os.environ.get("LOCAL_WORKER_LIVE_CLOUD") == "1"

    @classmethod
    def tearDownClass(cls):
        if not LIVE or not hasattr(cls, "observations"):
            return
        record = cls.record_path()
        record.parent.mkdir(parents=True, exist_ok=True)
        cls._write_record(record)

    @classmethod
    def record_path(cls):
        requested = os.environ.get("LOCAL_WORKER_QUALIFICATION_RECORD")
        if requested:
            return Path(requested).expanduser().resolve()
        date = _datetime.date.today().isoformat()
        host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-") or "host"
        return ROOT / "docs" / "qualification" / (date + "-" + host + ".md")

    @classmethod
    def _write_record(cls, path):
        profile = next((item.get("profile") for item in cls.observations if item.get("profile")), None)
        lines = ["# Task 26 live qualification — %s" % socket.gethostname(), "",
                 "Date: %s" % _datetime.datetime.now().astimezone().isoformat(),
                 "Platform: %s %s" % (platform.platform(), platform.machine()),
                 "Launcher: `%s`" % LAUNCHER,
                 "State: `%s`" % cls.state,
                 "Live mode: `LOCAL_WORKER_LIVE=1`; local jobs used `bin/local-worker run`.",
                 "Cloud inference: %s" % ("enabled by explicit opt-in" if cls.cloud_enabled
                                            else "not called; dry catalog and argv check only"),
                 "Structured read output: local and cloud read requests use `--output-schema`; "
                 "the launcher independently validates schema and citations.",
                 "Native plan: %s" % ("`%s`" % cls.native_plan if cls.native_plan else
                                      "not supplied; native case skipped"), "",
                 "## Discovered local profile", "",
                 "```json", json.dumps(profile, indent=2, sort_keys=True) if profile else
                 "No profile captured.", "```", "", "## Case results", "",
                 "| Case | Result | Duration (s) | Input tokens | Cached tokens | Output tokens | Evidence |",
                 "|---|---:|---:|---:|---:|---:|---|"]
        for item in cls.observations:
            tokens = item.get("tokens") or {}
            lines.append("| {name} | {status} | {duration} | {input} | {cached} | {output} | {evidence} |".format(
                name=item.get("name", "unknown"), status=item.get("status", "unknown"),
                duration=item.get("duration_seconds", "unavailable"),
                input=tokens.get("input_tokens", "unavailable"),
                cached=tokens.get("cached_input_tokens", "unavailable"),
                output=tokens.get("output_tokens", "unavailable"),
                evidence=(item.get("evidence", "").replace("|", "\\|").replace("\n", " "))))
        lines.extend(["", "Token counts are taken only from launcher reports; unavailable values remain marked.",
                      "Durations are measured by this harness around the case.", ""])
        path.write_text("\n".join(lines), encoding="utf-8")

    def run(self, result=None):
        if not LIVE:
            return super().run(result)
        cls = type(self)
        cls.current_started = time.monotonic()
        cls.current_name = self._testMethodName
        cls.current_metrics = {}
        result = result if result is not None else self.defaultTestResult()
        completed = super().run(result)
        # unittest.TestCase.run returns None for skipped methods on Python 3.9; preserve the
        # result instance so skipped opt-in live cases can still be recorded and the offline
        # suite can discover them without enabling LOCAL_WORKER_LIVE.
        if completed is not None:
            result = completed
        failed = any(test is self for test, _ in result.failures + result.errors)
        skipped = any(test is self for test, _ in result.skipped)
        cls.observations.append({
            "name": self._testMethodName,
            "status": "SKIP" if skipped else "FAIL" if failed else "PASS",
            "duration_seconds": round(time.monotonic() - cls.current_started, 3),
            "tokens": dict(cls.current_metrics.get("tokens") or {}),
            "evidence": cls.current_metrics.get("evidence", ""),
            **({"profile": cls.current_metrics["profile"]} if "profile" in cls.current_metrics else {}),
        })
        return result

    def run_job(self, task_path, *extra, timeout=1800):
        completed, compact = _json_command([
            LAUNCHER, "run", "--task-file", task_path, "--state-dir", self.state,
            "--json", *extra], timeout=timeout, env={"CODEX_THREAD_ID": self.session})
        report_path = (compact or {}).get("report_path")
        full = {}
        if report_path and Path(report_path).is_file():
            full = json.loads(Path(report_path).read_text(encoding="utf-8"))
        usage = full.get("usage") if isinstance(full.get("usage"), dict) else {}
        self.current_metrics["tokens"] = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
        self.current_metrics["evidence"] = "%s; report=%s" % (
            (compact or {}).get("status", "no JSON report"), report_path or "unavailable")
        self.reports.append(full or compact or {"stdout": completed.stdout[-1000:],
                                                "stderr": completed.stderr[-1000:]})
        return completed, compact or {}, full

    def test_discovery_matches_server(self):
        completed, payload = _json_command([LAUNCHER, "preflight"])
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("model_fallback"), False)
        model = payload.get("profile", {}).get("model", {})
        self.assertTrue(model.get("canonical_id"))
        self.assertTrue(model.get("api_model"))
        self.assertGreater(model.get("context_window", 0), 0)
        self.current_metrics["profile"] = model
        type(self).profile = model
        self.current_metrics["evidence"] = "preflight profile matched live server; fallback disabled"

    def test_read_pilot_citations_verified(self):
        fixture = self.root / ("read-pilot-" + self.session)
        if not fixture.exists():
            subprocess.run(["python3", str(PREPARE_READ), "--root", str(fixture)],
                           cwd=str(ROOT), check=True, timeout=60)
        task = fixture / "task.json"
        completed, compact, report = self.run_job(task, "--workstream", "task26-read",
                                                   "--job-kind", "implementation")
        self.assertIn(completed.returncode, (0,), completed.stderr + completed.stdout)
        self.assertEqual(compact.get("status"), "ok", report.get("failure_reason"))
        answer = report.get("answer") or {}
        self.assertIn("Qualification Widget", answer.get("title", ""), json.dumps(answer))
        citations = report.get("citation_verification", {})
        self.assertGreater(citations.get("total", 0), 0, json.dumps(citations))
        self.assertEqual(citations.get("verified"), citations.get("total"), json.dumps(citations))
        # A schema-valid answer and a quoted citation could be guessed from the prompt. Require
        # the retained Codex event stream to show an actual read command and its fixture content.
        attempts = report.get("attempts", [])
        event_logs = [path for attempt in attempts for path in attempt.get("logs", [])
                      if str(path).endswith(".jsonl")]
        inspections = []
        for event_log in event_logs:
            with open(event_log, encoding="utf-8") as stream:
                for line in stream:
                    event = json.loads(line)
                    item = event.get("item", {})
                    if (event.get("type") == "item.completed" and
                            item.get("type") == "command_execution" and
                            item.get("exit_code") == 0 and
                            'TITLE = "Qualification Widget"' in item.get("aggregated_output", "")):
                        inspections.append(item.get("command", ""))
        self.assertTrue(any("cat widget.py" in command for command in inspections),
                        "read pilot must inspect widget.py and expose its source in the real tool event")
        self.current_metrics["evidence"] += "; observed cat widget.py returning the cited TITLE source line"

    def test_write_pilot_ok_inside_wrapper(self):
        fixture = self.root / ("write-pilot-" + self.session)
        if not fixture.exists():
            subprocess.run(["python3", str(PREPARE_WRITE), "--root", str(fixture)],
                           cwd=str(ROOT), check=True, timeout=60)
        completed, compact, report = self.run_job(fixture / "task.json",
                                                   "--workstream", "task26-write")
        self.assertEqual(compact.get("status"), "ok", report.get("failure_reason"))
        self.assertTrue(report.get("permissions", {}).get("deny_list_applied"),
                        json.dumps(report.get("permissions")))
        self.assertFalse(report.get("primary_changed_during_job"))

    def test_forced_context_overflow_is_environment(self):
        fixture = self.root / ("read-pilot-" + self.session)
        if not fixture.exists():
            subprocess.run(["python3", str(PREPARE_READ), "--root", str(fixture)],
                           cwd=str(ROOT), check=True, timeout=60)
        task = json.loads((fixture / "task.json").read_text(encoding="utf-8"))
        task["task_id"] = "task26-context-overflow"
        completed, profile = _json_command([LAUNCHER, "preflight"], timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        context_window = profile["profile"]["model"]["context_window"]
        # Stay under the installed Codex turn/start 1 MiB character ceiling while making
        # each space-delimited numeric token lexically distinct and well above model context.
        token_count = int(context_window * 1.7)
        digits = random.Random(26025)
        oversized_tokens = " ".join(str(digits.randrange(10)) for _ in range(token_count))
        self.assertLess(len(oversized_tokens) + 1000, 1_000_000,
                        "overflow request must stay under Codex turn/start character ceiling")
        task["question"] = "This request intentionally exceeds the discovered model context. " + oversized_tokens
        oversized = self.root / ("context-overflow-" + self.session + ".json")
        oversized.write_text(json.dumps(task), encoding="utf-8")
        completed, compact, report = self.run_job(oversized, "--context-cap", "4096",
                                                   "--workstream", "task26-context")
        classification = compact.get("classification") or report.get("classification") or {}
        self.assertEqual(classification.get("class"), "environment", json.dumps(classification))
        self.assertEqual(classification.get("code"), "context_overflow", json.dumps(classification))
        self.assertNotEqual(compact.get("status"), "ok")

    def test_forced_capability_failure_offers_escalation(self):
        fixture = self.root / ("impossible-pilot-" + self.session)
        repo = fixture / "repo"
        if not fixture.exists():
            subprocess.run(["python3", str(PREPARE_WRITE), "--root", str(fixture)],
                           cwd=str(ROOT), check=True, timeout=60)
            (repo / "test_add.py").write_text(
                "import unittest\nfrom add import add\nclass Impossible(unittest.TestCase):\n"
                " def test_incompatible_constraints(self):\n  self.assertEqual(add(2, 3), 5)\n"
                "  self.assertEqual(add(2, 3), 6)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "test_add.py"], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Task26",
                            "-c", "user.email=task26@localhost", "commit", "-m", "impossible test"],
                           check=True, timeout=30)
            task = json.loads((fixture / "task.json").read_text(encoding="utf-8"))
            task["task_id"] = "task26-impossible"
            task["base_ref"] = "HEAD"
            task["allowed_paths"] = ["add.py"]
            (fixture / "task.json").write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
        completed, compact, report = self.run_job(fixture / "task.json",
                                                   "--workstream", "task26-capability")
        classification = compact.get("classification") or report.get("classification") or {}
        self.assertNotEqual(compact.get("status"), "ok")
        self.assertIn(classification.get("class"), ("capability", "blocked"),
                      json.dumps(classification))
        self.assertIn(classification.get("next_action"), ("escalate_cloud", "planner", "needs_user"),
                      json.dumps(classification))

    def test_cloud_escalation_argv(self):
        completed, catalog = _json_command([LAUNCHER, "models", "--cloud", "--json"], timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIsInstance(catalog, list)
        self.assertTrue(catalog)
        candidate = catalog[0]
        self.assertTrue(candidate.get("slug"))
        self.assertTrue(candidate.get("reasoning_levels"))
        # Exercise deterministic tier/argv construction without invoking the cloud worker.
        from pathlib import Path as _Path
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "src"))
        import local_worker as lw
        effort = candidate["reasoning_levels"][0]
        state = _Path(self.root / "cloud-dry-state")
        worktree = state / "worktrees" / "dry"
        worktree.mkdir(parents=True, exist_ok=True)
        source_home = _Path(self.root / "cloud-auth-home")
        permission_config = lw.native_read_profile_overrides(
            lw.permissions.default_permissions(), state, worktree,
            extra_deny_read=[source_home])
        schema = _Path(self.root / "cloud-read-schema.json")
        schema.write_text(json.dumps(lw.read_answers.envelope_schema({})), encoding="utf-8")
        argv = lw.cloud_worker_argv(
            "codex", worktree, candidate["slug"], effort,
            _Path(self.root / "last-message.json"), sandbox_mode=None,
            output_schema=schema, permission_config=permission_config, strict_config=True)
        self.assertEqual(lw.cloud_policy_check(
            argv, candidate["slug"], effort, sandbox_mode=None, output_schema=schema,
            permission_config=permission_config, strict_config=True), [])
        self.assertNotIn("-s", argv)
        self.assertIn("--strict-config", argv)
        self.current_metrics["evidence"] = (
            "catalog=%s; dry cloud argv passed native read-profile and output-schema self-check" %
            candidate["slug"])
        if self.cloud_enabled:
            self.fail("LOCAL_WORKER_LIVE_CLOUD=1 requires controller authorization for cloud inference")

    def test_native_before_after_screenshots(self):
        if not self.native_plan:
            self.skipTest("set LOCAL_WORKER_LIVE_NATIVE_PLAN to a reviewed native plan")
        plan = Path(self.native_plan).expanduser().resolve()
        payload = json.loads(plan.read_text(encoding="utf-8"))
        simulator = os.environ.get("LOCAL_WORKER_LIVE_NATIVE_SIMULATOR_UDID")
        self.assertTrue(simulator, "set LOCAL_WORKER_LIVE_NATIVE_SIMULATOR_UDID for owned simulator")
        fixture = Path(os.environ.get("LOCAL_WORKER_LIVE_NATIVE_TASK", ""))
        self.assertTrue(fixture.is_file(), "set LOCAL_WORKER_LIVE_NATIVE_TASK to native write task")
        completed, compact, report = self.run_job(fixture, "--simulator-udid", simulator,
            "--native-preflight-plan", plan, "--workstream", "task26-native", timeout=3600)
        self.assertEqual(compact.get("status"), "ok", report.get("failure_reason"))
        expected_test = "-only-testing:NativePilotUITests/NativePilotUITests/testAppShowsAfter"
        verification = report.get("tests", [])
        after_tests = [item for item in verification
                       if expected_test in item.get("command", [])]
        self.assertTrue(after_tests, "native verification must select the After assertion test")
        self.assertTrue(all(item.get("status") == "passed" and item.get("returncode") == 0
                            for item in after_tests), json.dumps(after_tests))
        source_names = set((report.get("screenshots", {}).get("sources") or {}).keys())
        self.assertTrue({"before", "after", "reference"}.issubset(source_names), str(source_names))
        entries = report.get("screenshots", {}).get("sources", {})
        self.assertTrue(all(item.get("path") and not item.get("uniform")
                            for source in ("before", "after") for item in entries[source]),
                        json.dumps(entries))
        methods = {item.get("method") for results in entries.values() for item in results}
        self.assertTrue({"simctl", "xcresult"}.issubset(methods), json.dumps(entries))
        self.current_metrics["evidence"] = "native XCTest verification selected %s and passed; before/after/reference screenshots via simctl and named XCTest attachment in %s" % (
            expected_test.rsplit("/", 1)[-1],
            report.get("screenshots", {}).get("index"))

    def test_summary_written(self):
        completed, output = _json_command([LAUNCHER, "summary", "--session", self.session,
                                           "--state-dir", self.state], timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("Wrote ", completed.stdout)
        self.assertTrue(list((self.state / "sessions" / self.session).glob("summary.*")))
        self.current_metrics["evidence"] = "summary output persisted under %s" % (
            self.state / "sessions" / self.session)


if __name__ == "__main__":
    unittest.main(verbosity=2)
