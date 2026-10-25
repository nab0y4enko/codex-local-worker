"""Classify launcher-observed failures and recommend a bounded next action.

Only structured launcher evidence is inspected. In particular, ``failure_reason`` and worker
messages are deliberately not parsed: model prose is not evidence about why a job failed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

ENVIRONMENT_CODES = (
    "server_unreachable", "server_loading", "model_not_loaded", "model_ambiguous",
    "model_changed", "context_overflow", "server_memory", "tool_unavailable",
    "secret_read_denied", "sandbox_denied", "missing_env", "missing_tool", "simulator_unavailable",
    "stall", "budget_exhausted", "policy_refused", "verification_blocked",
    "deny_list_unavailable", "interrupted", "worker_busy",
)
CAPABILITY_CODES = ("tests_failed", "out_of_scope", "no_change", "invalid_answer",
                    "unverified_answer")
NEXT_ACTIONS = ("done", "retry_local", "escalate_cloud", "needs_user")


@dataclass
class Classification:
    cls: str
    code: str
    evidence: List[str]
    remediation: str
    next_action: str
    plugin_suggestion: Optional[str]
    auto_fix: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        result = {"class": self.cls, "code": self.code, "evidence": list(self.evidence),
                  "remediation": self.remediation, "next_action": self.next_action,
                  "plugin_suggestion": self.plugin_suggestion}
        if self.auto_fix is not None:
            result["auto_fix"] = dict(self.auto_fix)
        return result


_MESSAGES = {
    "server_unreachable": ("The local model server stopped responding.", "retry_local", {"wait_for_server": 1}),
    "server_loading": ("Wait until the local model server finishes loading, then retry.", "needs_user", None),
    "model_not_loaded": ("Load one chat model in oMLX, then retry.", "needs_user", None),
    "model_ambiguous": ("Select the intended loaded chat model and retry.", "needs_user", None),
    "model_changed": ("Restore the pinned model in oMLX or start a fresh job.", "needs_user", None),
    "context_overflow": ("Retry with a smaller context cap.", "retry_local", None),
    "server_memory": ("Free local model-server memory or reduce the request size.", "needs_user", None),
    "tool_unavailable": ("The installed worker tool surface does not provide a requested tool.", "retry_local", None),
    "secret_read_denied": ("Review the denied path and explicitly allow it only if needed.", "needs_user", None),
    "sandbox_denied": ("A sandboxed command was denied. Review the reported path and command; keep the current permission policy unless a human decides a specific access change is needed.", "needs_user", None),
    "missing_env": ("Provide the required environment variable through the task contract.", "needs_user", None),
    "missing_tool": ("Install or expose the missing verification tool, then retry.", "needs_user", None),
    "simulator_unavailable": ("Make the selected simulator available and retry.", "needs_user", None),
    "stall": ("The worker stopped producing events; inspect the server and retry if healthy.", "retry_local", None),
    "budget_exhausted": ("Increase the job budget or reduce its scope.", "needs_user", None),
    "policy_refused": ("Review the launcher policy refusal before retrying.", "needs_user", None),
    "worker_busy": ("Wait and retry, or pass --lock-timeout with enough job budget to wait for the worker.", "retry_local", None),
    "verification_blocked": ("Make the required verification command runnable and retry.", "needs_user", None),
    "deny_list_unavailable": ("Restore the deny wrapper before running this job.", "needs_user", None),
    "interrupted": ("Review the preserved partial job and start a new task if appropriate.", "needs_user", None),
    "tests_failed": ("Review the failed verification and revise the implementation.", "escalate_cloud", None),
    "out_of_scope": ("Revert edits outside the contract's allowed paths.", "escalate_cloud", None),
    "no_change": ("Revise the task or implementation so it produces an in-scope change.", "escalate_cloud", None),
    "invalid_answer": ("Correct the answer schema and retry.", "escalate_cloud", None),
    "unverified_answer": ("Repair the citations so each answer claim is verifiable.", "escalate_cloud", None),
}


def _texts(report: Dict[str, Any]) -> List[str]:
    """Return structured error strings already extracted by the launcher."""
    values = []
    attempts = [item for item in report.get("attempts", []) or [] if isinstance(item, dict)]
    # The last attempt decides the current retry decision; an earlier failure remains in the
    # report's history but must not mask a new, distinct failure code.
    for attempt in attempts[-1:]:
        for key in ("error_text", "typed_error_text"):
            value = attempt.get(key)
            if isinstance(value, str):
                values.append(value.lower())
            elif isinstance(value, list):
                values.extend(item.lower() for item in value if isinstance(item, str))
    return values


def _pick(report: Dict[str, Any]) -> tuple:
    status = report.get("status")
    if status == "worker_busy":
        return "worker_busy", ["launcher status=worker_busy; job was not started"]
    discovery = report.get("discovery_error")
    if isinstance(discovery, dict) and discovery.get("code") in ENVIRONMENT_CODES:
        return discovery["code"], ["discovery_error.code=" + discovery["code"]]

    citations = report.get("citation_verification") or report.get("citations")
    if status == "answer_invalid" and isinstance(citations, dict):
        total, verified = citations.get("total"), citations.get("verified")
        if citations.get("failures") or (isinstance(total, int) and isinstance(verified, int) and verified < total):
            return "unverified_answer", ["citation verification failed (%s/%s verified)" % (verified, total)]
    if isinstance(citations, dict) and citations.get("failures"):
        return "unverified_answer", ["launcher recorded failed citation verification"]

    attempts = [item for item in report.get("attempts", []) or [] if isinstance(item, dict)]
    if attempts:
        denial = attempts[-1].get("sandbox_denial")
        if (isinstance(denial, dict) and denial.get("source") in (
                    "sandboxed_command_output", "codex_native_readonly_command",
                    "codex_terminal_error")
                and denial.get("kind") in ("read", "write")
                and isinstance(denial.get("path"), str)):
            if denial["kind"] == "read" and denial.get("known_secret") is True:
                return "secret_read_denied", ["launcher promoted a typed read denial for a configured secret path"]
            evidence = ["launcher promoted a typed sandbox command denial"]
            if denial["kind"] == "write" and denial.get("known_out_of_scope") is True:
                evidence.append("the blocked write targeted a protected out-of-scope path")
            return "sandbox_denied", evidence
    if status == "audit_violation" or report.get("violations"):
        return "out_of_scope", ["launcher audit recorded an out-of-scope violation"]
    if status == "no_changes":
        return "no_change", ["launcher status=no_changes"]
    if status in ("interrupted",):
        return "interrupted", ["launcher status=interrupted"]
    if status in ("timeout", "budget_exhausted") or (report.get("budget") or {}).get("exceeded"):
        return "budget_exhausted", ["launcher budget evidence indicates exhaustion"]
    for test in report.get("tests", []) or []:
        if not isinstance(test, dict):
            continue
        output = str(test.get("stdout_tail", "") or "") + " " + str(test.get("reason", "") or "")
        if test.get("returncode") == 127 or "command not found" in output.lower():
            return "missing_tool", ["verification command was missing (exit 127 or command not found)"]
    if status == "verification_blocked":
        for test in report.get("tests", []) or []:
            if not isinstance(test, dict):
                continue
            output = str(test.get("stdout_tail", "") or "") + " " + str(test.get("reason", "") or "")
            if test.get("status") == "blocked" and "environment" in output.lower():
                return "missing_env", ["verification evidence reports a missing environment value"]
        return "verification_blocked", ["launcher status=verification_blocked"]

    attempts = [item for item in report.get("attempts", []) or [] if isinstance(item, dict)]
    latest = attempts[-1] if attempts else {}
    if latest.get("interrupted"):
        return "interrupted", ["attempt record marks interruption"]
    if latest.get("timed_out"):
        return "budget_exhausted", ["attempt record marks timeout"]
    if latest.get("stalled"):
        return "stall", ["attempt record marks stalled"]
    texts = _texts(report)
    combined = " ".join(texts)
    if any(token in combined for token in ("context length", "maximum context", "prompt is too long")):
        return "context_overflow", ["launcher error evidence reports context overflow"]
    if "507" in combined or "memory" in combined:
        return "server_memory", ["launcher error evidence reports server memory pressure"]
    if "connection refused" in combined or "os error 61" in combined:
        return "server_unreachable", ["launcher error evidence reports connection refusal"]
    if "unsupported call" in combined:
        return "tool_unavailable", ["launcher error evidence reports unsupported call"]
    if status == "answer_invalid":
        return "invalid_answer", ["launcher status=answer_invalid"]
    if (status == "worker_failed" and attempts
            and isinstance(attempts[-1].get("returncode"), int)
            and attempts[-1]["returncode"] != 0):
        if report.get("kind", report.get("job_kind")) == "read":
            return "invalid_answer", ["launcher observed a failed read worker without a valid answer"]
        if report.get("worker_patch_present") is False:
            return "no_change", ["launcher observed a failed worker exit with no surviving patch"]
        if report.get("worker_patch_present") is True:
            return "tests_failed", ["launcher observed a failed worker exit after producing a patch"]
    if status == "tests_failed":
        return "tests_failed", ["verification status=tests_failed"]
    if status == "preflight_failed":
        return "server_unreachable", ["launcher status=preflight_failed"]
    if status == "ok" or status == "ready":
        return "ok", ["launcher status=" + str(status)]
    if status == "blocked" and report.get("policy_stop"):
        return "policy_refused", ["launcher workstream policy stopped the job"]
    return "verification_blocked", ["launcher status=" + str(status or "unknown") + " without a more specific failure signal"]


def classify(report: Dict[str, Any]) -> Classification:
    if not isinstance(report, dict):
        raise TypeError("report must be a dictionary")
    code, evidence = _pick(report)
    if code == "ok":
        return Classification("none", "ok", evidence, "", "done", None)
    cls = "environment" if code in ENVIRONMENT_CODES else "capability"
    remediation, action, auto_fix = _MESSAGES[code]
    previous = report.get("previous_codes", []) or []
    if action == "retry_local" and code in previous:
        action, auto_fix = "needs_user", None
    if code == "context_overflow":
        window = report.get("context_window")
        if isinstance(window, int) and not isinstance(window, bool) and window > 1:
            auto_fix = {"context_cap": max(1, int(window * .75))}
    suggestion = None
    if code == "context_overflow":
        suggestion = "Base context compaction on the effective context limit and reserve space for the system prompt."
    elif code == "stall" and report.get("unknown_rates"):
        suggestion = "Improve stall detection when token throughput is unavailable."
    elif code == "tool_unavailable":
        suggestion = "Update the worker tool configuration to expose the tool required by the prompt."
    return Classification(cls, code, evidence, remediation, action, suggestion, auto_fix)
