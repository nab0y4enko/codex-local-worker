#!/usr/bin/env python3
"""Deterministic qualification controller: plan, explicit live campaign, offline report/audit.

Stdlib only. One controller call replaces bespoke cloud-written setup/account/report turns:

  run    --output-dir ABS           pure dry run: plan only, no writes, no subprocesses,
                                    no model discovery, no auth/config access
  run    --live --output-dir ABS    explicit opt-in; fresh directory only, a source checkout
                                    with Git metadata, the four frozen campaign scripts staged
                                    and run sequentially, then report.md rendered offline from
                                    the evidence just written; a nonzero stage exits nonzero
  report --evidence-dir ABS         deterministic Markdown from existing per-task evidence
  audit  --snapshot ABS_JSON        reconcile an existing parent-overhead snapshot offline

`report` and `audit` never launch a subprocess, discover models, read credentials or private
homes, and never turn missing usage into zero. Frozen tasks, arms, budgets, checks, reviewer
decisions, accounting and archives live in the copied stage templates and stay unchanged here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SOURCE_ENV = "LOCAL_WORKER_QUALIFICATION_SOURCE"
STAGES: Tuple[str, ...] = ("setup", "run", "account", "finalize")
TASKS: Tuple[str, ...] = ("slug", "stats")
ARMS: Tuple[str, ...] = ("direct_cloud", "launcher", "direct_luna")
RESULTS_NAME = "per-task-results.json"
AGGREGATE_NAME = "aggregate.json"
SNAPSHOT_NAME = "parent-overhead-snapshot.json"
COUNTER_RECORDS_NAME = "parent-counter-records.json"
REPORT_NAME = "report.md"
STAGE_RECEIPTS_NAME = "qualification-stage-receipts.json"
RUN_RECEIPT_NAME = "qualification-run.json"
UNKNOWN = "unavailable"
COMPLETED_STATUSES = frozenset({"accepted"})
KNOWN_STATUSES = frozenset({"accepted", "rejected", "failed", "decision_failed"})
USAGE_KEYS: Tuple[str, ...] = ("uncached_input", "cached_input", "output", "total_cloud_tokens")
# Frozen execution order retained verbatim from the verified campaign (stats runs reversed).
EXECUTION_ORDER: Tuple[Tuple[str, str], ...] = (
    ("slug", "direct_cloud"), ("slug", "launcher"), ("slug", "direct_luna"),
    ("stats", "direct_luna"), ("stats", "launcher"), ("stats", "direct_cloud"))
BUDGETS: Dict[str, int] = {
    "implementation_seconds": 600,
    "local_internal_attempts": 2,
    "additional_semantic_repairs": 0,
    "review_seconds": 300,
    "planning_seconds": 120,
}
UNAVAILABLE_REASON = ("Parent-overhead auto-collection is not portable for staged runs and is "
                      "reported unavailable, never zero. Use `audit --snapshot` on an existing "
                      "snapshot to reconcile it.")

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = Path(__file__).resolve().parent / "qualification_templates"
_PERSONAL_PATH = re.compile(r"/Users/")
_THREAD_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


class QualificationError(RuntimeError):
    """Refusal that stops the controller before anything is written or executed."""


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _number(value: Any, label: str, *, allow_float: bool = False) -> Optional[float]:
    """Accept recorded usage only; missing stays None and invalid input is a hard failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise QualificationError(f"{label} is not numeric; usage is never guessed")
    if isinstance(value, int) and value >= 0:
        return value
    if allow_float and isinstance(value, float) and value >= 0:
        return value
    raise QualificationError(f"{label} is not a non-negative number; usage is never guessed")


def _format(value: Any, *, decimal: bool = False) -> str:
    if value is None:
        return UNKNOWN
    return f"{value:,.1f}" if decimal else f"{value:,}"


def template_path(stage: str) -> Path:
    if stage not in STAGES:
        raise QualificationError(f"unknown stage: {stage}")
    path = TEMPLATES_DIR / f"{stage}.py.txt"
    if not path.is_file():
        raise QualificationError(f"missing stage template: {path}")
    return path


def portability_problems(source: str) -> List[str]:
    """Non-portable leftovers that must never appear in a staged campaign script."""
    problems: List[str] = []
    if _PERSONAL_PATH.search(source):
        problems.append("personal absolute path")
    if _THREAD_ID.search(source):
        problems.append("hardcoded thread/rollout id")
    try:
        compile(source, "staged", "exec")
    except SyntaxError as error:
        problems.append(f"syntax error: {error.msg}")
    return problems


def check_templates() -> Dict[str, List[str]]:
    """Per-stage portability and compile problems for the templates this controller stages."""
    return {stage: portability_problems(template_path(stage).read_text()) for stage in STAGES}


def resolve_source(explicit: Optional[str] = None) -> Path:
    value = explicit or os.environ.get(SOURCE_ENV) or str(REPO_ROOT)
    return Path(os.path.abspath(str(Path(str(value)).expanduser())))


def _contained(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _real_root(path: Path) -> Path:
    """Filesystem truth for containment: a symlinked parent must not escape the check."""
    try:
        return Path(os.path.realpath(str(path)))
    except OSError:
        return Path(os.path.abspath(str(path)))


def validate_output_dir(output_dir: str, source: Path) -> Path:
    path = Path(str(output_dir)).expanduser()
    if not str(path) or not path.is_absolute():
        raise QualificationError("--output-dir must be an absolute path")
    resolved = Path(os.path.abspath(str(path)))
    real = _real_root(resolved)
    for label, root in (("addon checkout", REPO_ROOT), ("qualification source checkout", source)):
        if _contained(resolved, root) or _contained(real, _real_root(root)):
            raise QualificationError(f"refusing evidence output inside the {label}: {resolved}")
    if resolved.exists() or resolved.is_symlink():
        raise QualificationError(
            f"refusing to overwrite or restart an existing campaign evidence dir: {resolved}")
    return resolved


def stage_commands(stage_dir: Path, python: Optional[str] = None) -> Dict[str, List[str]]:
    interpreter = python or sys.executable or "python3"
    return {stage: [interpreter, str(stage_dir / f"{stage}.py")] for stage in STAGES}


def build_plan(output_dir: str, *, live: bool = False,
               source: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic campaign plan; writes nothing and starts no process."""
    source_root = resolve_source(source)
    evidence_dir = validate_output_dir(output_dir, source_root)
    commands = stage_commands(evidence_dir)
    return {
        "mode": "live" if live else "dry-run",
        "source": str(source_root),
        "source_env": SOURCE_ENV,
        "output_dir": str(evidence_dir),
        "evidence_dir": str(evidence_dir),
        "results_path": str(evidence_dir / RESULTS_NAME),
        "report_path": str(evidence_dir / REPORT_NAME),
        "tasks": list(TASKS),
        "arms": list(ARMS),
        "execution_order": [[task, arm] for task, arm in EXECUTION_ORDER],
        "budgets": dict(BUDGETS),
        "stages": list(STAGES),
        "stage_commands": {stage: list(commands[stage]) for stage in STAGES},
        "protections": [
            "fresh evidence directory only; existing campaigns are never reused or restarted",
            "evidence output may not live inside the addon or qualification source checkout",
            "containment resolves symlinked parents, so a link cannot place evidence inside a "
            "protected checkout",
            "live --source must be a Git checkout (a .git directory or a linked-worktree .git "
            "file); an installed cache is refused before anything is created or staged",
            "frozen checks, budgets, reviewer decisions and accounting come from the templates",
            "a nonzero stage exit code stops the campaign and is retained as evidence",
            "report.md is rendered offline from the evidence of the campaign whose stages all "
            "passed; no stage supplies narrative figures",
            "dry run performs no writes, subprocesses, model discovery or auth access",
        ],
        "outer_overhead": UNAVAILABLE_REASON,
    }


def execute_stage(argv: Sequence[str], cwd: Path, env: Dict[str, str],
                  log_path: Path) -> int:
    """Run one staged campaign script with a durable log; return its exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        completed = subprocess.run([str(part) for part in argv], cwd=str(cwd), env=dict(env),
                                   stdout=handle, stderr=subprocess.STDOUT, text=True)
    return int(completed.returncode)


def _require_source_tree(source: Path) -> None:
    if not source.is_dir():
        raise QualificationError(f"qualification source checkout is missing: {source}")
    for relative in ("bin/local-worker", "src/local_worker.py", "examples/matched-benchmark"):
        if not (source / relative).exists():
            raise QualificationError(f"qualification source checkout lacks {relative}: {source}")


def _require_source_git_metadata(source: Path) -> None:
    """Live freezes the campaign to ``git rev-parse HEAD``, so an installed cache is not a source.

    Filesystem metadata only -- no git subprocess, no model discovery and no credential or
    private-home access -- and it runs before any directory, stage copy or model call, so a
    refusal leaves nothing behind and a dry run never needs it.
    """
    git = source / ".git"
    if not git.is_dir() and not git.is_file():
        raise QualificationError(
            f"live source is not a Git checkout (no .git directory or linked-worktree .git "
            f"file): {source}; the campaign freezes its production commit, so use --source "
            "with a real source checkout")


def run_campaign(output_dir: str, *, live: bool = False, source: Optional[str] = None,
                 execute: Optional[Callable[..., int]] = None,
                 stdout: Any = None) -> Dict[str, Any]:
    """Print the plan; only ``live`` may create a fresh directory and run the frozen stages."""
    out = stdout if stdout is not None else sys.stdout
    plan = build_plan(output_dir, live=live, source=source)
    out.write("mode: " + plan["mode"] + "\n")
    out.write("source: " + plan["source"] + "\n")
    out.write("evidence_dir: " + plan["evidence_dir"] + "\n")
    out.write("report: " + plan["report_path"] + "\n")
    out.write("budgets: " + json.dumps(plan["budgets"], sort_keys=True) + "\n")
    out.write("stages: " + " -> ".join(plan["stages"]) + "\n")
    out.write("stage commands:\n")
    for stage in STAGES:
        out.write("  " + stage + ": " + json.dumps(plan["stage_commands"][stage]) + "\n")
    out.write("outer overhead: " + UNAVAILABLE_REASON + "\n")
    if not live:
        out.write("dry run: no directory created, nothing staged, no subprocess, no model or "
                  "auth access\n")
        return dict(plan, executed=False, planned_only=True, stage_exit_codes={})

    source_root = Path(plan["source"])
    evidence_dir = Path(plan["evidence_dir"])
    _require_source_tree(source_root)
    _require_source_git_metadata(source_root)
    problems = {stage: issues for stage, issues in check_templates().items() if issues}
    if problems:
        raise QualificationError(f"staged templates are not portable: {problems}")
    try:
        evidence_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise QualificationError(
            f"evidence dir appeared during launch; refusing to reuse it: {evidence_dir}") from error
    for stage in STAGES:
        shutil.copyfile(template_path(stage), evidence_dir / f"{stage}.py")
    env = dict(os.environ)
    env[SOURCE_ENV] = str(source_root)
    runner = execute or execute_stage
    commands = stage_commands(evidence_dir)
    codes: Dict[str, int] = {}
    receipts: List[Dict[str, Any]] = []
    receipt_path = evidence_dir / STAGE_RECEIPTS_NAME
    failed: Optional[str] = None
    for stage in STAGES:
        log_path = evidence_dir / "stage-logs" / f"{stage}.log"
        returncode = int(runner(commands[stage], evidence_dir, env, log_path))
        codes[stage] = returncode
        receipts.append({"stage": stage, "argv": commands[stage], "returncode": returncode,
                         "log_path": str(log_path)})
        receipt_path.write_text(_dump({"stage_exit_codes": codes, "stages": receipts}) + "\n",
                                encoding="utf-8")
        if returncode != 0:
            failed = stage
            break
    result = dict(plan, executed=True, stage_exit_codes=codes, receipts=receipts,
                  stage_receipts_path=str(receipt_path), incomplete=failed is not None,
                  failed_stage=failed)
    out.write("stage exit codes: " + json.dumps(codes, sort_keys=True) + "\n")
    out.write("evidence: " + str(evidence_dir) + "\n")
    if failed is not None:
        out.write(f"stopped at {failed} (exit {codes[failed]}); partial evidence retained, "
                  "frozen budgets and campaign state are not reset\n")
        result["report_rendered"] = False
        (evidence_dir / RUN_RECEIPT_NAME).write_text(_dump(result) + "\n", encoding="utf-8")
        out.write("report: " + plan["report_path"] + "\n")
        return result
    # Every frozen stage passed: render the report from the evidence that this campaign just
    # wrote. The offline renderer is the single source of figures, so no stage can keep a
    # narrative number from an earlier study, and a report failure is reported, not hidden.
    result["report_rendered"] = True
    (evidence_dir / RUN_RECEIPT_NAME).write_text(_dump(result) + "\n", encoding="utf-8")
    try:
        rendered = build_report(str(evidence_dir))
    except QualificationError as error:
        raise QualificationError(
            f"every stage passed but the offline report could not be rendered from "
            f"{evidence_dir}: {error}") from error
    (evidence_dir / REPORT_NAME).write_text(rendered, encoding="utf-8")
    out.write("report: " + plan["report_path"] + "\n")
    return result


def _load_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise QualificationError(f"missing {label}: {path}")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise QualificationError(f"invalid {label} at {path}: {error}") from error


def load_results(evidence_dir: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Read and validate existing per-task evidence, plus aggregate accounting when present."""
    root = Path(str(evidence_dir)).expanduser()
    if not root.is_absolute():
        raise QualificationError("--evidence-dir must be an absolute path")
    results_path = root / RESULTS_NAME
    rows = _load_json(results_path, "per-task results")
    if not isinstance(rows, list) or not rows:
        raise QualificationError(f"per-task results must be a non-empty list: {results_path}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise QualificationError(f"per-task result {index} is not an object")
        for key in ("task", "arm", "status"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise QualificationError(f"per-task result {index} is missing {key!r}")
        if row["arm"] not in ARMS:
            raise QualificationError(f"per-task result {index} has unknown arm {row['arm']!r}")
        if row["status"] not in KNOWN_STATUSES:
            raise QualificationError(
                f"per-task result {index} has unknown status {row['status']!r}")
    aggregate = None
    aggregate_path = root / AGGREGATE_NAME
    if aggregate_path.is_file():
        aggregate = _load_json(aggregate_path, "aggregate accounting")
        if not isinstance(aggregate, dict):
            raise QualificationError(f"aggregate accounting must be an object: {aggregate_path}")
    return rows, aggregate


def row_usage(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    label = f"{row['task']}/{row['arm']}"
    usage: Dict[str, Optional[float]] = {key: _number(row.get(key), f"{label} {key}")
                                         for key in USAGE_KEYS}
    usage["seconds"] = _number(row.get("seconds"), f"{label} seconds", allow_float=True)
    categories = [usage[key] for key in ("uncached_input", "cached_input", "output")]
    total = usage["total_cloud_tokens"]
    if total is not None and all(value is not None for value in categories):
        # A recorded total is kept as recorded and a missing category is never filled in, so the
        # identity is checked only where every figure needed for it was actually supplied.
        expected = sum(categories)
        if abs(float(total) - float(expected)) > 1e-6:
            raise QualificationError(
                f"{label} total_cloud_tokens={total} is not uncached_input+cached_input+output"
                f"={expected}; a recorded total is never reconciled by guessing")
    return usage


def arm_accounting(rows: List[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    """Charge rejected and failed work to the arm; divide only by completed tasks."""
    selected = [row for row in rows if row["arm"] == arm]
    usages = [row_usage(row) for row in selected]
    completed = sum(1 for row in selected if row["status"] in COMPLETED_STATUSES)
    rejected = sum(1 for row in selected if row["status"] == "rejected")
    failed = len(selected) - completed - rejected

    def total(key: str) -> Optional[float]:
        values = [usage[key] for usage in usages]
        if not values or any(value is None for value in values):
            return None
        return sum(values)

    tokens = total("total_cloud_tokens")
    attempted = len(selected)
    return {
        "arm": arm,
        "attempted_tasks": attempted,
        "completed_tasks": completed,
        "rejected_tasks": rejected,
        "failed_tasks": failed,
        "total_cloud_tokens_including_rejections": tokens,
        "tokens_per_attempted_task": tokens / attempted if tokens is not None and attempted
        else None,
        "tokens_per_completed_task_including_failed_cost":
            tokens / completed if tokens is not None and completed else None,
        "total_seconds_including_rejections": total("seconds"),
        "usage_unavailable": any(usage[key] is None for usage in usages
                                 for key in USAGE_KEYS + ("seconds",)),
    }


def _cross_check_aggregate(rows: List[Dict[str, Any]], aggregate: Dict[str, Any]) -> None:
    """Never reconcile against a stale or partial accounting file."""
    for arm, entry in aggregate.items():
        if arm not in ARMS:
            raise QualificationError(f"aggregate accounting has unknown arm {arm!r}")
        if not isinstance(entry, dict):
            raise QualificationError(f"aggregate accounting for {arm} is not an object")
        computed = arm_accounting(rows, arm)
        for key in ("attempted_tasks", "completed_tasks", "rejected_tasks", "failed_tasks",
                    "total_cloud_tokens_including_rejections"):
            expected, actual = computed[key], entry.get(key)
            if expected is None or actual is None:
                continue
            if actual != expected:
                raise QualificationError(
                    f"aggregate {arm} {key}={actual!r} disagrees with per-task evidence "
                    f"{expected!r}")
        expected = computed["tokens_per_completed_task_including_failed_cost"]
        actual = entry.get("tokens_per_completed_task_including_failed_cost")
        if expected is not None and actual is not None and abs(float(actual) - expected) > 1e-6:
            raise QualificationError(
                f"aggregate {arm} per-completed-task cost={actual!r} disagrees with per-task "
                f"evidence {expected!r}")


def parent_overhead(evidence_root: Path) -> Dict[str, Any]:
    """Snapshot figures when they exist; otherwise explicitly unavailable, never zero."""
    path = evidence_root / SNAPSHOT_NAME
    if not path.is_file():
        return {"availability": UNKNOWN, "reason": UNAVAILABLE_REASON}
    snapshot = _load_json(path, "parent overhead snapshot")
    counts = snapshot.get("counts") if isinstance(snapshot, dict) else None
    if not isinstance(counts, dict) or not counts:
        return {"availability": UNKNOWN, "reason": UNAVAILABLE_REASON, "snapshot": str(path)}
    reconciled = {key: _number(counts.get(key), f"parent overhead {key}")
                  for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                              "total_tokens")}
    return {"availability": "snapshot", "as_of": snapshot.get("as_of"),
            "completeness": snapshot.get("completeness", "snapshot"), "snapshot": str(path),
            "counts": reconciled}


def measured_cloud_total(accounting: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Overall measured cloud total; unknown usage in any attempted arm keeps it unknown.

    Summing an unknown arm as zero would understate the campaign and produce an all-zero claim
    for a campaign that only has missing usage, so an incomplete campaign reports the known
    subtotal as a subtotal and the overall figure as unavailable.
    """
    attempted = [entry for entry in accounting.values() if entry["attempted_tasks"]]
    totals = [entry["total_cloud_tokens_including_rejections"] for entry in attempted]
    unknown_arms = [entry["arm"] for entry, total in zip(attempted, totals) if total is None]
    known = [total for total in totals if total is not None]
    subtotal = sum(known) if known else None
    if not attempted:
        text = f"{UNKNOWN} (no attempted task)"
    elif not unknown_arms:
        text = _format(subtotal)
    elif subtotal is None:
        text = (f"{UNKNOWN} (every attempted arm has missing usage: {', '.join(unknown_arms)}; "
                "never summed as zero)")
    else:
        text = (f"{UNKNOWN} (known subtotal {_format(subtotal)} from the arms with complete "
                f"usage; unknown remainder from {', '.join(unknown_arms)}, not counted as zero)")
    return {"subtotal": subtotal, "complete": bool(attempted) and not unknown_arms,
            "unknown_arms": unknown_arms, "text": text}


def build_report(evidence_dir: str) -> str:
    """Deterministic Markdown for existing evidence; no subprocess, model or credential access."""
    root = Path(str(evidence_dir)).expanduser()
    rows, aggregate = load_results(evidence_dir)
    if aggregate is not None:
        _cross_check_aggregate(rows, aggregate)
    usages = [row_usage(row) for row in rows]
    missing = sum(1 for usage in usages
                  for key in USAGE_KEYS + ("seconds",) if usage[key] is None)
    accounting = {arm: arm_accounting(rows, arm) for arm in ARMS}
    measured = measured_cloud_total(accounting)
    lines: List[str] = ["# Deterministic qualification report", ""]
    lines += [f"- Evidence: `{root}`",
              f"- Results: `{root / RESULTS_NAME}`",
              f"- Tasks: {', '.join(TASKS)}; arms: {', '.join(ARMS)}; one repetition per "
              "task/arm, in the frozen order.",
              "- Cloud totals are input plus output; cached input is a subset of input and is "
              "counted once, and reasoning is already included in output.",
              f"- Missing usage stays {UNKNOWN}; it is never reported or summed as zero.", ""]
    lines += ["## Every attempted task, failures included", "",
              "| Task | Arm | Outcome | Uncached input | Cached input | Output | "
              "Total cloud tokens | Seconds |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row, usage in zip(rows, usages):
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["task"], row["arm"], row["status"], _format(usage["uncached_input"]),
            _format(usage["cached_input"]), _format(usage["output"]),
            _format(usage["total_cloud_tokens"]), _format(usage["seconds"], decimal=True)))
    lines += ["", "## Completed-task accounting, charged with rejected and failed work", "",
              "| Arm | Attempted | Completed | Rejected | Failed | All cloud tokens spent | "
              "Per attempted task | Per completed task, rejected work included |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm in ARMS:
        entry = accounting[arm]
        if not entry["attempted_tasks"]:
            continue
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            arm, entry["attempted_tasks"], entry["completed_tasks"], entry["rejected_tasks"],
            entry["failed_tasks"], _format(entry["total_cloud_tokens_including_rejections"]),
            _format(entry["tokens_per_attempted_task"], decimal=True),
            _format(entry["tokens_per_completed_task_including_failed_cost"], decimal=True)))
    overhead = parent_overhead(root)
    lines += ["", "## Outer experiment overhead", ""]
    if overhead["availability"] == "snapshot":
        counts = overhead["counts"]
        lines += [f"Parent chat snapshot as of {overhead.get('as_of')}: input "
                  f"{_format(counts['input_tokens'])} (cached subset "
                  f"{_format(counts['cached_input_tokens'])}), output "
                  f"{_format(counts['output_tokens'])}, total "
                  f"{_format(counts['total_tokens'])}. Measured task-arm cloud total is "
                  f"{measured['text']}. Overhead is reported separately and is not charged "
                  "selectively to an arm; reconcile with `audit --snapshot`.", ""]
    else:
        lines += [f"Parent overhead is {UNKNOWN}: {UNAVAILABLE_REASON} Measured task-arm cloud "
                  f"total is {measured['text']}; no assumed-zero overhead is subtracted from "
                  "it.", ""]
    lines += ["## Notes", "",
              f"- Rows with missing usage fields: {missing}. They render as {UNKNOWN} and are "
              "excluded from arm totals instead of counted as zero.",
              f"- Overall measured task-arm cloud total: {measured['text']}. A known subtotal is "
              "never presented as the campaign total while any attempted arm still has missing "
              "usage.",
              "- No failure or semantic rejection is omitted; rejected work stays charged to its "
              "arm while the denominator counts only completed tasks.",
              "- Rendered offline: no subprocess, model call, model discovery or credential/"
              "private-home access."]
    return "\n".join(lines) + "\n"


def audit_parent_overhead(snapshot_path: str) -> Dict[str, Any]:
    """Reconcile an existing parent-overhead snapshot with its sibling counter records.

    Offline and count-only: the snapshot baseline is the first delta, cached input is a subset of
    input, reasoning is a subset of output, and total is input plus output exactly once.
    """
    path = Path(str(snapshot_path)).expanduser()
    if not path.is_absolute():
        raise QualificationError("--snapshot must be an absolute path")
    snapshot = _load_json(path, "parent overhead snapshot")
    if not isinstance(snapshot, dict):
        raise QualificationError(f"parent overhead snapshot must be an object: {path}")
    counts = snapshot.get("counts")
    baseline = snapshot.get("baseline_total_usage")
    if not isinstance(counts, dict) or not isinstance(baseline, dict):
        raise QualificationError(
            f"snapshot needs both 'counts' and 'baseline_total_usage': {path}")
    records_path = path.with_name(COUNTER_RECORDS_NAME)
    records = _load_json(records_path, "parent counter records")
    if not isinstance(records, list) or not records:
        raise QualificationError(
            f"parent counter records must be a non-empty list: {records_path}")
    counters: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        usage = record.get("total_token_usage") if isinstance(record, dict) else None
        if not isinstance(usage, dict):
            raise QualificationError(f"counter record {index} has no total_token_usage")
        counters.append(usage)
    keys = sorted(set(baseline) & set(counters[0]))
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        if key not in keys:
            raise QualificationError(f"snapshot counter records are missing {key}")
    monotonic = all(baseline[key] <= counters[0][key] for key in keys) and all(
        all(counters[index - 1][key] <= counters[index][key] for key in keys)
        for index in range(1, len(counters)))
    deltas = [{key: counters[0][key] - baseline[key] for key in keys}]
    deltas += [{key: counters[index][key] - counters[index - 1][key] for key in keys}
               for index in range(1, len(counters))]
    inputs = [delta["input_tokens"] for delta in deltas]

    def derived(key: str) -> Optional[int]:
        return counters[-1][key] - baseline[key] if key in keys else None

    input_total = _number(counts.get("input_tokens"), "snapshot counts.input_tokens")
    cached_input = _number(counts.get("cached_input_tokens"), "snapshot counts.cached_input_tokens")
    output_total = _number(counts.get("output_tokens"), "snapshot counts.output_tokens")
    total_tokens = _number(counts.get("total_tokens"), "snapshot counts.total_tokens")
    reasoning = derived("reasoning_output_tokens")
    checks = {
        "counters_monotonic": monotonic,
        "first_delta_uses_snapshot_baseline":
            deltas[0]["input_tokens"] == counters[0]["input_tokens"] - baseline["input_tokens"],
        "input_delta_matches_counts": sum(inputs) == input_total,
        "cached_input_delta_matches_counts":
            sum(delta["cached_input_tokens"] for delta in deltas) == cached_input,
        "output_delta_matches_counts": sum(delta["output_tokens"] for delta in deltas)
        == output_total,
        "cached_input_counted_once_as_subset": (cached_input is not None
                                               and input_total is not None
                                               and cached_input <= input_total),
        "reasoning_not_added_twice": (reasoning is not None and output_total is not None
                                      and reasoning <= output_total),
        "total_is_input_plus_output": (total_tokens is not None and input_total is not None
                                       and output_total is not None
                                       and total_tokens == input_total + output_total),
    }
    return {
        "status": "reconciled" if all(checks.values()) else "mismatch",
        "snapshot": str(path),
        "counter_records": str(records_path),
        "completeness": snapshot.get("completeness", "snapshot"),
        "request_timestamp": snapshot.get("request_timestamp"),
        "as_of": snapshot.get("as_of") or records[-1].get("timestamp"),
        "responses": len(counters),
        "input_tokens": input_total,
        "cached_input_tokens": cached_input,
        "new_input_tokens": (input_total - cached_input
                             if input_total is not None and cached_input is not None else None),
        "output_tokens": output_total,
        "reasoning_output_tokens": reasoning,
        "visible_output_tokens": (output_total - reasoning if output_total is not None
                                  and reasoning is not None else None),
        "total_tokens": total_tokens,
        "first_input_delta_from_baseline": deltas[0]["input_tokens"],
        "median_input_per_response": statistics.median(inputs),
        "min_input_per_response": min(inputs),
        "max_input_per_response": max(inputs),
        "scope": snapshot.get("scope"),
        "private_reasoning_bodies": "not read",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-worker-qualification",
        description="Deterministic qualification controller: plan or explicitly run a staged "
                    "campaign, and render offline reports/audits from existing evidence.")
    sub = parser.add_subparsers(dest="command", metavar="{run,report,audit}")
    sub.required = True
    run_parser = sub.add_parser("run", help="plan a campaign; --live is required to execute")
    run_parser.add_argument("--output-dir", required=True,
                            help="absolute fresh evidence directory")
    run_parser.add_argument("--live", action="store_true",
                            help="explicit opt-in: create the directory and run the stages")
    run_parser.add_argument("--source", default=None,
                            help=f"source checkout; defaults to ${SOURCE_ENV} then this checkout")
    report_parser = sub.add_parser("report", help="offline Markdown from existing evidence")
    report_parser.add_argument("--evidence-dir", required=True)
    audit_parser = sub.add_parser("audit", help="offline parent-overhead reconciliation")
    audit_parser.add_argument("--snapshot", required=True,
                              help=f"existing {SNAPSHOT_NAME}; needs sibling "
                                   f"{COUNTER_RECORDS_NAME}")
    return parser


def main(argv: Optional[Sequence[str]] = None, stdout: Any = None,
         execute: Optional[Callable[..., int]] = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout
    try:
        if arguments.command == "run":
            result = run_campaign(arguments.output_dir, live=arguments.live,
                                  source=arguments.source, execute=execute, stdout=out)
            if result.get("incomplete"):
                stage = result.get("failed_stage")
                code = result.get("stage_exit_codes", {}).get(stage)
                out.write(f"run: campaign incomplete; stage {stage} exited {code}, later "
                          "stages did not run and nothing was restarted; stage receipts and "
                          "partial evidence are retained as written\n")
                return 1
            return 0
        if arguments.command == "report":
            out.write(build_report(arguments.evidence_dir))
            return 0
        audit = audit_parent_overhead(arguments.snapshot)
        out.write(_dump(audit) + "\n")
        return 0 if audit["status"] == "reconciled" else 1
    except QualificationError as error:
        out.write(f"local-worker-qualification {arguments.command}: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
