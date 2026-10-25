"""Matched token accounting for recorded benchmark runs; never invokes models.

Manifest version 1 pairs equal task and acceptance specifications. Each arm names
one final session summary, end-to-end elapsed seconds, and the number of semantic
review decisions. Launcher arms also name every raw job report, the accepted job's
review package and decision-result receipt, and the accepted job ID. Direct arms
with no worker reports use the final cloud rollout plus explicit external semantic
acceptance evidence. The frozen specification contains goal, base reference,
acceptance criteria, test commands, and a SHA-256 of trusted acceptance-check pins
(or null when none are used); its own canonical JSON SHA-256 must match. Session
summaries prove ledger integrity and final planner usage; raw reports preserve
attempts, failures, and job-level usage. Counts are read once from job reports
(which already sum all worker attempts) and the final planner snapshot. No
inference, dollar estimate, or subscription claim is performed here. See
examples/benchmark.json for the version 1 shape.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple


MAX_FILE_BYTES = 8 * 1024 * 1024
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
ARM_NAMES = ("direct_cloud", "launcher", "direct_luna")


class BenchmarkError(ValueError):
    """Invalid manifest or evidence that cannot support a comparison."""


def _read(path: Any) -> Dict[str, Any]:
    if not isinstance(path, str) or not path:
        raise BenchmarkError("evidence paths must be non-empty strings")
    file = Path(path).expanduser().absolute()
    if file.is_symlink() or not file.is_file():
        raise BenchmarkError("evidence is missing or not a regular file: %s" % file)
    if file.stat().st_size > MAX_FILE_BYTES:
        raise BenchmarkError("evidence exceeds size limit: %s" % file)
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise BenchmarkError("cannot read JSON evidence %s: %s" % (file, error)) from error
    if not isinstance(value, dict):
        raise BenchmarkError("evidence must be a JSON object: %s" % file)
    return value


def _count(value: Any) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def acceptance_spec_sha256(criteria: Any) -> str:
    """Fingerprint a frozen specification using stable compact JSON encoding."""
    encoded = json.dumps(criteria, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _complete_counts(values: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    result = {}
    for name in TOKEN_FIELDS:
        parts = [_count(value.get(name)) for value in values]
        result[name] = sum(parts) if parts and all(item is not None for item in parts) else None
    return result


def _package_check_definitions(package: Dict[str, Any]):
    """Verify trusted pins and return stable identities plus receipt commands."""
    contract = package.get("contract")
    contract = contract if isinstance(contract, dict) else {}
    manifest_path = contract.get("acceptance_checks")
    if manifest_path is None:
        return None, None
    if not isinstance(manifest_path, str) or not Path(manifest_path).is_absolute():
        raise BenchmarkError("review package has an invalid trusted-check manifest path")
    supplied_manifest = Path(os.path.abspath(manifest_path))
    if supplied_manifest.is_symlink():
        raise BenchmarkError("trusted-check manifest must not be a symlink")
    manifest_file = Path(os.path.realpath(str(supplied_manifest)))
    canonical_manifest_path = str(manifest_file)
    bindings = package.get("bindings")
    if not isinstance(bindings, dict) or canonical_manifest_path not in bindings:
        raise BenchmarkError("trusted-check manifest is not bound by the review package")
    if not manifest_file.is_file():
        raise BenchmarkError("trusted-check manifest is unavailable")
    manifest_bytes = manifest_file.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != bindings[canonical_manifest_path]:
        raise BenchmarkError("trusted-check manifest changed since review")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise BenchmarkError("trusted-check manifest is invalid: %s" % error) from error
    if (not isinstance(manifest, dict) or manifest.get("version") != 1 or
            not isinstance(manifest.get("checks"), list)):
        raise BenchmarkError("trusted-check manifest has an unsupported shape")
    semantic_checks = []
    receipt_checks = []
    for check in manifest["checks"]:
        if (not isinstance(check, dict) or set(check) != {"name", "argv", "scripts"} or
                not isinstance(check["argv"], list) or not isinstance(check["scripts"], list)):
            raise BenchmarkError("trusted-check definition is incomplete")
        script_hashes = []
        normalized_script_paths = set()
        normalized_argv = list(check["argv"])
        for raw_path in check["scripts"]:
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise BenchmarkError("trusted-check script path is invalid")
            supplied_script = Path(os.path.abspath(raw_path))
            if supplied_script.is_symlink():
                raise BenchmarkError("trusted-check script must not be a symlink")
            script = Path(os.path.realpath(str(supplied_script)))
            canonical_path = str(script)
            normalized_script_paths.add(canonical_path)
            try:
                argv_index = normalized_argv.index(raw_path)
            except ValueError as error:
                raise BenchmarkError("trusted-check script must appear in its argv") from error
            normalized_argv[argv_index] = canonical_path
            if canonical_path not in bindings:
                raise BenchmarkError("trusted-check script is not bound by the review package")
            if not script.is_file():
                raise BenchmarkError("trusted-check script is unavailable")
            content_hash = hashlib.sha256(script.read_bytes()).hexdigest()
            if content_hash != bindings[canonical_path]:
                raise BenchmarkError("trusted-check script changed since review")
            script_hashes.append(content_hash)
        semantic_argv = ["<trusted-script>" if item in normalized_script_paths else item
                         for item in normalized_argv]
        semantic_checks.append({"name": check["name"], "argv": semantic_argv,
                                "scripts_sha256": script_hashes})
        receipt_checks.append({"name": check["name"], "command": normalized_argv})
    return semantic_checks, receipt_checks


def _package_checks_sha256(package: Dict[str, Any]) -> Optional[str]:
    """Hash trusted check meaning and script bytes, excluding run-specific paths."""
    semantic_checks, _receipt_checks = _package_check_definitions(package)
    if semantic_checks is None:
        return None
    return acceptance_spec_sha256({"version": 1, "checks": semantic_checks})


def _review_record(record: Dict[str, Any], task_id: str, spec: Dict[str, Any],
                   job_id: str, action_required: str,
                   report_path: Optional[str] = None) -> Tuple[bool, List[str]]:
    issues = []
    try:
        package = _read(record.get("package"))
        receipt = _read(record.get("receipt"))
    except BenchmarkError as error:
        return False, [str(error)]
    contract = package.get("contract")
    contract = contract if isinstance(contract, dict) else {}
    decision = receipt.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    if package.get("job_id") != job_id or receipt.get("job_id") != job_id:
        issues.append("review package/receipt does not identify job %s" % job_id)
    if report_path is not None:
        try:
            expected_report = Path(report_path).expanduser().resolve(strict=True)
            packaged_report = Path(package.get("report_path", "")).expanduser().resolve(strict=True)
            bindings = package.get("bindings")
            if (expected_report != packaged_report or not isinstance(bindings, dict) or
                    bindings.get(str(expected_report)) != hashlib.sha256(
                        expected_report.read_bytes()).hexdigest()):
                issues.append("review package is not bound to the raw report being counted")
        except (OSError, TypeError, ValueError):
            issues.append("review package report binding is missing or invalid")
    if contract.get("task_id") != task_id:
        issues.append("review package task identity does not match")
    for key in ("goal", "base_ref", "acceptance_criteria", "test_commands"):
        if contract.get(key) != spec.get(key):
            issues.append("review package %s does not match frozen specification" % key)
    if package.get("base_commit") != spec.get("base_commit"):
        issues.append("review package base commit does not match frozen source identity")
    receipt_checks = None
    try:
        semantic_checks, receipt_checks = _package_check_definitions(package)
        checks_hash = (acceptance_spec_sha256({"version": 1, "checks": semantic_checks})
                       if semantic_checks is not None else None)
    except BenchmarkError as error:
        checks_hash = None
        issues.append(str(error))
    if checks_hash != spec.get("acceptance_checks_sha256"):
        issues.append("review package trusted-check pins do not match frozen specification")
    if package.get("semantic_review_required") is not True:
        issues.append("review package lacks semantic review requirement")
    if action_required == "accept" and package.get("complete") is not True:
        issues.append("accepted review package is incomplete")
    expected_status = "accepted" if action_required == "accept" else "rejected"
    if receipt.get("status") != expected_status or decision.get("action") != action_required:
        issues.append("semantic review receipt does not record %s" % action_required)
    if action_required == "accept" and package.get("kind") == "write":
        if receipt.get("applied") is not True:
            issues.append("accepted write was not recorded as applied")
        integration = receipt.get("integration_tests")
        if not isinstance(integration, list) or not integration or any(
                not isinstance(test, dict) or test.get("status") != "passed"
                for test in integration):
            issues.append("accepted write lacks successful combined integration checks")
        else:
            required_commands = contract.get("test_commands")
            if not isinstance(required_commands, list) or not required_commands:
                issues.append("accepted write has no original verification commands")
            elif any(not any(test.get("command") == command and test.get("status") == "passed"
                             for test in integration) for command in required_commands):
                issues.append("combined integration evidence omits an original verification command")
        if receipt_checks:
            if not isinstance(integration, list) or any(
                    not any(test.get("name") == check["name"] and
                            test.get("command") == check["command"] and
                            test.get("status") == "passed"
                            for test in integration if isinstance(test, dict))
                    for check in receipt_checks):
                issues.append("combined integration evidence omits a trusted acceptance check")
        archive = receipt.get("archive")
        if not isinstance(archive, dict) or archive.get("status") != "archived" or archive.get("removed") is not True:
            issues.append("accepted write lacks a completed archive receipt")
    if (decision.get("semantic_review") is not True or
            decision.get("reviewer") != "cloud-planner"):
        issues.append("receipt lacks explicit cloud semantic acceptance")
    required_sections = {"diff", "sources", "tests", "findings", "audit", "screenshots"}
    if not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip():
        issues.append("semantic review rationale is missing")
    if not required_sections.issubset(set(decision.get("reviewed_sections", []))):
        issues.append("semantic review did not cover all required sections")
    if not package.get("package_sha256") or decision.get("package_sha256") != package.get("package_sha256"):
        issues.append("review receipt is not bound to the package digest")
    unsigned = dict(package)
    claimed = unsigned.pop("package_sha256", None)
    actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")).hexdigest()
    if claimed != actual:
        issues.append("review package digest does not match package contents")
    return not issues, issues


def _arm_metrics(name: str, arm: Dict[str, Any], task_id: str,
                 specification: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[str] = []
    try:
        summary = _read(arm.get("summary"))
        paths = arm.get("reports", [])
        if not isinstance(paths, list):
            raise BenchmarkError("arm reports must be a list of every launcher job report")
        reports = [_read(path) for path in paths]
    except BenchmarkError as error:
        return {"complete": False, "issues": [str(error)], "cloud_tokens": {
            "cached_input": None, "uncached_input": None, "output": None},
            "attempts": None, "failed_attempts": None, "repairs": None, "rejected_jobs": None,
            "elapsed_seconds": None, "accepted": False}

    if summary.get("ledger_integrity") != "complete":
        issues.append("session ledger is missing or incomplete")
    rows = summary.get("jobs")
    if not isinstance(rows, list):
        rows = []
        issues.append("session summary has no job list")
    reports_by_id = {}
    report_paths_by_id = {}
    for raw_path, report in zip(paths, reports):
        job_id = report.get("job_id")
        contract = report.get("contract")
        report_task = report.get("task_id") or (
            contract.get("task_id") if isinstance(contract, dict) else None)
        if not isinstance(job_id, str) or not job_id or job_id in reports_by_id:
            issues.append("job reports have a missing or duplicate job ID")
            continue
        reports_by_id[job_id] = report
        report_paths_by_id[job_id] = raw_path
        if report.get("tier") not in ("local", "cloud"):
            issues.append("job report %s has an unknown worker tier" % job_id)
        if report_task != task_id:
            issues.append("job report %s task identity does not match" % job_id)
        if report.get("base_commit") != specification.get("base_commit"):
            issues.append("job report %s base commit does not match frozen source identity" % job_id)
        usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
        if usage.get("complete") is not True:
            issues.append("job report %s has incomplete token usage" % job_id)
        if not isinstance(report.get("attempts"), list) or not report.get("attempts"):
            issues.append("job report %s has missing attempt history" % job_id)
    row_ids = [row.get("job_id") for row in rows if isinstance(row, dict)]
    if len(row_ids) != len(rows) or set(row_ids) != set(reports_by_id) or len(row_ids) != len(set(row_ids)):
        issues.append("raw reports do not cover every job in the final session summary exactly once")
    row_by_id = {row["job_id"]: row for row in rows if isinstance(row, dict) and
                 isinstance(row.get("job_id"), str)}
    for job_id, report in reports_by_id.items():
        row = row_by_id.get(job_id)
        if row is None:
            continue
        usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
        expected = {"input": _count(usage.get("input_tokens")),
                    "cached": _count(usage.get("cached_input_tokens")),
                    "output": _count(usage.get("output_tokens"))}
        if row.get("tier") != report.get("tier") or row.get("status") != report.get("status"):
            issues.append("session ledger row disagrees with raw report %s" % job_id)
        row_tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        observed = {key: row_tokens.get(key) for key in ("input", "cached", "output")}
        if observed != expected:
            issues.append("session ledger token totals disagree with raw report %s" % job_id)

    # The session aggregate must independently agree with the one-count-per-job
    # reconstruction. Ledger totals contain job aggregates, never separate retry rows.
    reported_totals = summary.get("totals")
    if not isinstance(reported_totals, dict):
        reported_totals = {}
        issues.append("session summary has no tier totals")
    tiers = {report.get("tier") for report in reports}
    for tier in tiers:
        tier_reports = [report for report in reports if report.get("tier") == tier]
        expected = _complete_counts([
            {"input_tokens": (r.get("usage") or {}).get("input_tokens"),
             "cached_input_tokens": (r.get("usage") or {}).get("cached_input_tokens"),
             "output_tokens": (r.get("usage") or {}).get("output_tokens")}
            for r in tier_reports])
        summary_tier = reported_totals.get(tier)
        if not isinstance(summary_tier, dict):
            issues.append("session summary is missing totals for tier %s" % tier)
            continue
        for field in TOKEN_FIELDS:
            if summary_tier.get(field) != expected[field]:
                issues.append("session tier totals disagree with raw reports for %s" % tier)

    if name != "launcher" and reports:
        issues.append("direct comparison arms must use rollout usage and contain no worker reports")
    accepted_id = arm.get("accepted_job_id")
    semantic_rejections = None
    external_metrics = None
    if name == "launcher":
        if accepted_id not in reports_by_id:
            issues.append("accepted job is absent from launcher reports")
            accepted = False
        else:
            accepted_record = dict(arm, package=arm.get("package"), receipt=arm.get("receipt"))
            accepted, review_issues = _review_record(accepted_record, task_id, specification,
                                                     accepted_id, "accept",
                                                     report_paths_by_id[accepted_id])
            issues.extend(review_issues)
        review_records = arm.get("review_records")
        if not isinstance(review_records, list):
            review_records = []
            issues.append("review_records must list the semantic decision for every other job")
        review_map = {}
        for review in review_records:
            if not isinstance(review, dict) or not isinstance(review.get("job_id"), str):
                issues.append("semantic review record has no job ID")
                continue
            reviewed_id = review["job_id"]
            if reviewed_id in review_map or reviewed_id == accepted_id:
                issues.append("semantic review record has a duplicate or accepted job ID")
                continue
            review_map[reviewed_id] = review
            if reviewed_id not in reports_by_id:
                issues.append("semantic review record refers to an absent job %s" % reviewed_id)
                continue
            valid, review_issues = _review_record(review, task_id, specification,
                                                  reviewed_id, "reject",
                                                  report_paths_by_id[reviewed_id])
            issues.extend(review_issues)
        expected_rejected_ids = set(reports_by_id) - ({accepted_id} if accepted_id else set())
        if set(review_map) != expected_rejected_ids:
            issues.append("every non-accepted job requires its semantic rejection receipt")
        semantic_rejections = len(review_map) if set(review_map) == expected_rejected_ids else None
    elif accepted_id is None:
        try:
            acceptance = _read(arm.get("acceptance"))
        except BenchmarkError as error:
            acceptance = {}
            issues.append(str(error))
        accepted = True
        if (acceptance.get("task_id") != task_id or
                acceptance.get("session") != summary.get("session") or
                acceptance.get("specification_sha256") != acceptance_spec_sha256(specification)):
            issues.append("external acceptance evidence does not match task or session identity")
        if acceptance.get("status") != "accepted" or acceptance.get("semantic_review") is not True:
            issues.append("external semantic acceptance evidence is not accepted")
        if (acceptance.get("reviewer") != "cloud-planner" or
                not isinstance(acceptance.get("rationale"), str) or
                not acceptance["rationale"].strip()):
            issues.append("external acceptance lacks explicit cloud reviewer and rationale")
        semantic_rejections = 0
        external_metrics = acceptance.get("metrics")
        metric_fields = ("attempts", "failed_attempts", "failed_jobs", "rejected_jobs",
                         "semantic_rejections",
                         "repairs", "repair_jobs", "automatic_local_retries",
                         "review_calls", "elapsed_seconds")
        if not isinstance(external_metrics, dict) or any(
                (type(external_metrics.get(field)) is not int if field != "elapsed_seconds"
                 else type(external_metrics.get(field)) not in (int, float)) or
                not math.isfinite(external_metrics[field]) or external_metrics[field] < 0
                for field in metric_fields):
            issues.append("external acceptance must record complete non-negative run metrics")
            external_metrics = None
        elif external_metrics["rejected_jobs"] != external_metrics["semantic_rejections"]:
            issues.append("external rejected-job and semantic-rejection counts disagree")
            semantic_rejections = None
        if external_metrics is not None:
            semantic_rejections = external_metrics["semantic_rejections"]
    else:
        issues.append("direct arms use external acceptance evidence, not worker decision receipts")
        accepted = False
        semantic_rejections = None

    # Reports are per-job aggregates that already include every worker attempt.
    # Sum each job exactly once; do not add per-attempt usage a second time.
    cloud_reports = [report for report in reports if report.get("tier") == "cloud"]
    cloud_job_counts = _complete_counts([
        {"input_tokens": (r.get("usage") or {}).get("input_tokens"),
         "cached_input_tokens": (r.get("usage") or {}).get("cached_input_tokens"),
         "output_tokens": (r.get("usage") or {}).get("output_tokens")}
        for r in cloud_reports])
    if not cloud_reports:
        cloud_job_counts = {field: 0 for field in TOKEN_FIELDS}
    planner = summary.get("planner") if isinstance(summary.get("planner"), dict) else None
    planner_overlap = False
    if name == "launcher" or not reports:
        if planner is None or planner.get("completeness") != "final":
            issues.append("final cloud rollout usage is missing or not final")
            planner_counts = {field: None for field in TOKEN_FIELDS}
        else:
            planner_counts = {field: _count(planner.get(field)) for field in TOKEN_FIELDS}
            if any(value is None for value in planner_counts.values()):
                issues.append("final planner token usage has unknown fields")
            subagents = planner.get("subagents")
            included_sessions = (set(subagents.get("sessions", []))
                                 if isinstance(subagents, dict) and
                                 isinstance(subagents.get("sessions"), list) else set())
            attempt_threads = {attempt.get("thread_id") for report in reports
                               for attempt in report.get("attempts", [])
                               if isinstance(attempt, dict) and
                               isinstance(attempt.get("thread_id"), str)}
            if included_sessions.intersection(attempt_threads):
                planner_overlap = True
                issues.append("planner usage includes a worker attempt thread; token overlap cannot be separated")
    else:
        planner_counts = {field: 0 for field in TOKEN_FIELDS}

    cloud_counts = {}
    for field in TOKEN_FIELDS:
        left, right = cloud_job_counts[field], planner_counts[field]
        cloud_counts[field] = left + right if left is not None and right is not None else None
    if planner_overlap:
        cloud_counts = {field: None for field in TOKEN_FIELDS}
    if name != "launcher" and reports:
        cloud_counts = {field: None for field in TOKEN_FIELDS}
    input_count = cloud_counts["input_tokens"]
    cached_count = cloud_counts["cached_input_tokens"]
    if (input_count is None or cached_count is None or cached_count > input_count):
        uncached = None
        if cached_count is not None and input_count is not None and cached_count > input_count:
            issues.append("cached input exceeds total input")
    else:
        uncached = input_count - cached_count
    token_output = cloud_counts["output_tokens"]
    attempt_rows = []
    for report in reports:
        report_attempts = report.get("attempts")
        if not isinstance(report_attempts, list):
            continue
        if any(not isinstance(attempt, dict) for attempt in report_attempts):
            issues.append("job report %s has an invalid attempt record" % report.get("job_id"))
        attempt_rows.extend(attempt for attempt in report_attempts if isinstance(attempt, dict))
    attempts = len(attempt_rows)
    def failed_attempt(attempt):
        tests = attempt.get("tests")
        failed_test = isinstance(tests, list) and any(
            not isinstance(test, dict) or test.get("status") != "passed" for test in tests)
        checks = attempt.get("acceptance_checks")
        failed_check = isinstance(checks, dict) and checks.get("status") != "passed"
        return (attempt.get("returncode") not in (0, None) or attempt.get("terminal_failures") or
                attempt.get("timed_out") or attempt.get("stalled") or attempt.get("interrupted") or
                failed_test or failed_check)
    failed_attempts = sum(1 for attempt in attempt_rows if failed_attempt(attempt))
    # The final report may carry trusted-check evidence outside per-attempt rows.
    for report in reports:
        checks = report.get("acceptance_checks")
        tests = report.get("tests")
        report_failed = ((isinstance(checks, dict) and checks.get("status") not in ("passed", None)) or
                         (isinstance(tests, list) and any(
                             not isinstance(test, dict) or test.get("status") != "passed"
                             for test in tests)))
        if report_failed and not any(failed_attempt(item) for item in report.get("attempts", [])
                                     if isinstance(item, dict)):
            failed_attempts += 1
    failed_jobs = sum(1 for report in reports if report.get("status") not in ("ok", "ready", "accepted"))
    if reports and all(report.get("job_kind") in ("implementation", "repair") for report in reports):
        repair_jobs = sum(1 for report in reports if report.get("job_kind") == "repair")
        automatic_local_retries = sum(max(0, len(report.get("attempts", [])) - 1)
                                       for report in reports if report.get("tier") == "local" and
                                       isinstance(report.get("attempts"), list))
        repairs = repair_jobs + automatic_local_retries
    else:
        repairs = arm.get("repairs") if not reports else None
        if type(repairs) is not int or repairs < 0:
            repairs = None
            issues.append("repair count is missing from evidence")
        repair_jobs = 0
        automatic_local_retries = 0
    if external_metrics is not None:
        attempts = int(external_metrics["attempts"])
        failed_attempts = int(external_metrics["failed_attempts"])
        failed_jobs = int(external_metrics["failed_jobs"])
        rejected_jobs = int(external_metrics["rejected_jobs"])
        semantic_rejections = int(external_metrics["semantic_rejections"])
        repairs = int(external_metrics["repairs"])
        repair_jobs = int(external_metrics["repair_jobs"])
        automatic_local_retries = int(external_metrics["automatic_local_retries"])
        review_calls = int(external_metrics["review_calls"])
        elapsed = float(external_metrics["elapsed_seconds"])
    elif name != "launcher":
        attempts = failed_attempts = failed_jobs = rejected_jobs = None
        semantic_rejections = repairs = repair_jobs = automatic_local_retries = None
        review_calls = elapsed = None
    if name == "launcher":
        elapsed = arm.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = None
            issues.append("end-to-end elapsed_seconds is missing or invalid")
        review_calls = arm.get("review_calls")
        if type(review_calls) is not int or review_calls < 1:
            review_calls = None
            issues.append("review_calls must record at least the accepted semantic review")
    complete = not issues and all(value is not None for value in (cached_count, uncached, token_output))
    return {"complete": complete, "issues": issues,
            "cloud_tokens": {"cached_input": cached_count, "uncached_input": uncached,
                             "output": token_output},
            "attempts": attempts, "failed_attempts": failed_attempts,
            "repairs": repairs, "repair_jobs": repair_jobs,
            "automatic_local_retries": automatic_local_retries, "failed_jobs": failed_jobs,
            "semantic_rejections": semantic_rejections,
            "rejected_jobs": semantic_rejections, "review_calls": review_calls,
            "elapsed_seconds": elapsed,
            "accepted": accepted}


def _arm_total(arm: Dict[str, Any]) -> Optional[int]:
    tokens = arm["cloud_tokens"]
    values = (tokens["cached_input"], tokens["uncached_input"], tokens["output"])
    return sum(values) if all(value is not None for value in values) else None


def compare(manifest_path: str) -> Dict[str, Any]:
    """Compare matched evidence pairs. Raises BenchmarkError for malformed manifests."""
    manifest = _read(manifest_path)
    if set(manifest) != {"version", "benchmark_id", "tasks"} or manifest.get("version") != 1:
        raise BenchmarkError("manifest must contain version 1, benchmark_id, and tasks only")
    if not isinstance(manifest.get("benchmark_id"), str) or not manifest["benchmark_id"]:
        raise BenchmarkError("benchmark_id must be a non-empty string")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise BenchmarkError("tasks must contain at least one matched task")
    results = []
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {
                "task_id", "specification", "specification_sha256", "arms"}:
            raise BenchmarkError("each task needs task_id, frozen specification, its SHA-256, and arms")
        task_id, specification, spec_hash, arms = (task["task_id"], task["specification"],
                                                    task["specification_sha256"], task["arms"])
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise BenchmarkError("task IDs must be non-empty and unique")
        seen.add(task_id)
        required_spec = {"goal", "base_ref", "base_commit", "acceptance_criteria", "test_commands",
                         "acceptance_checks_sha256"}
        if not isinstance(specification, dict) or set(specification) != required_spec:
            raise BenchmarkError("frozen specification needs goal, base_ref, base_commit, acceptance_criteria, "
                                 "test_commands, and acceptance_checks_sha256")
        if not isinstance(spec_hash, str) or spec_hash != acceptance_spec_sha256(specification):
            raise BenchmarkError("specification_sha256 does not match frozen specification")
        if not isinstance(arms, dict) or not {"direct_cloud", "launcher"}.issubset(arms):
            raise BenchmarkError("each task requires direct_cloud and launcher arms")
        if set(arms) - set(ARM_NAMES):
            raise BenchmarkError("unknown benchmark arm")
        metrics = {}
        for arm_name, arm in arms.items():
            if not isinstance(arm, dict):
                raise BenchmarkError("arm %s must be an object" % arm_name)
            metrics[arm_name] = _arm_metrics(arm_name, arm, task_id, specification)
        matched = all(value["complete"] and value["accepted"] for value in metrics.values())
        base, candidate = _arm_total(metrics["direct_cloud"]), _arm_total(metrics["launcher"])
        savings = ((base - candidate) / float(base) * 100.0
                   if matched and base not in (None, 0) and candidate is not None else None)
        results.append({"task_id": task_id, "matched": matched,
                        "issues": {key: value["issues"] for key, value in metrics.items()
                                   if value["issues"]},
                        "arms": metrics, "savings_percent": savings})
    all_matched = all(item["matched"] for item in results)
    totals = {}
    for arm_name in sorted({key for task in results for key in task["arms"]}):
        arms_for = [task["arms"].get(arm_name) for task in results]
        totals[arm_name] = {
            field: (sum(item["cloud_tokens"][field] for item in arms_for)
                    if all(item is not None and item["cloud_tokens"][field] is not None
                           for item in arms_for) else None)
            for field in ("cached_input", "uncached_input", "output")}
        totals[arm_name]["accepted_tasks"] = sum(1 for item in arms_for if item and item["accepted"])
        for field in ("attempts", "rejected_jobs", "semantic_rejections", "failed_jobs",
                      "failed_attempts", "repairs", "repair_jobs", "automatic_local_retries",
                      "review_calls", "elapsed_seconds"):
            values = [item.get(field) if item else None for item in arms_for]
            totals[arm_name][field] = (sum(values) if values and
                                       all(value is not None for value in values) else None)
    baseline_total = sum(totals["direct_cloud"][field] for field in ("cached_input", "uncached_input", "output")) if all(
        totals["direct_cloud"][field] is not None for field in ("cached_input", "uncached_input", "output")) else None
    candidate_total = sum(totals["launcher"][field] for field in ("cached_input", "uncached_input", "output")) if all(
        totals["launcher"][field] is not None for field in ("cached_input", "uncached_input", "output")) else None
    overall_savings = ((baseline_total - candidate_total) / float(baseline_total) * 100.0
                       if all_matched and baseline_total not in (None, 0) and candidate_total is not None else None)
    return {"version": 1, "benchmark_id": manifest["benchmark_id"], "tasks": results,
            "totals": totals, "all_tasks_matched": all_matched,
            "savings_percent": overall_savings,
            "claim": ("matched real evidence; token comparison only" if overall_savings is not None
                      else "no savings claim: pairs are incomplete, mismatched, or have a zero/unknown baseline")}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare matched real token evidence without model calls")
    parser.add_argument("--manifest", required=True, help="version 1 benchmark manifest JSON")
    parser.add_argument("--output", help="write result JSON to this path (default stdout)")
    args = parser.parse_args(argv)
    try:
        result = compare(args.manifest)
    except (BenchmarkError, OSError) as error:
        print("benchmark: %s" % error, file=sys.stderr)
        return 2
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0 if result["all_tasks_matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
