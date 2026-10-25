"""Session-scoped, redacted job ledger and human-readable summary."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple


SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
COUNT_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
_USAGE_KEYS = {
    "input_tokens": ("input_tokens",),
    "cached_input_tokens": ("cached_input_tokens",),
    "output_tokens": ("output_tokens",),
    "reasoning_tokens": ("reasoning_output_tokens", "reasoning_tokens"),
    "total_tokens": ("total_tokens",),
}
_PARENT_KEYS = ("parent_session_id", "parent_session", "parent_thread_id", "parent_id")


def session_id(explicit: Optional[str] = None) -> str:
    """Choose a session key without making up a job or thread identity."""
    value = explicit or os.environ.get("CODEX_THREAD_ID")
    if value:
        _validate_session(value)
        return value
    return "manual-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _validate_session(session: str) -> None:
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session) or session in (".", ".."):
        raise ValueError("session id must be a safe single path component")


def _number(value: Any) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _safe_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Copy only stable, non-prose fields; never persist worker output or credentials."""
    model = report.get("api_model")
    profile = report.get("model_profile")
    if not model and isinstance(profile, dict):
        model = profile.get("model")
        if isinstance(model, dict):
            model = model.get("canonical_id") or model.get("api_model")
        if not model and isinstance(profile.get("cloud"), dict):
            model = profile["cloud"].get("slug")
    if not model:
        model = "not reported"
    token_counts = report.get("token_counts")
    token_counts = token_counts if isinstance(token_counts, dict) else {}
    usage = report.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = {
        "input": _number(token_counts.get("input_tokens")),
        "cached": _number(usage.get("cached_input_tokens")),
        "output": _number(token_counts.get("output_tokens")),
        "reasoning": _number(usage.get("reasoning_output_tokens")),
    }
    timings = report.get("timing")
    timings = timings if isinstance(timings, dict) else {}
    attempts = [item for item in report.get("attempts", []) if isinstance(item, dict)]
    review_findings = report.get("review_findings")
    classification = report.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    screenshots = report.get("screenshots")
    screenshot_index = screenshots.get("index") if isinstance(screenshots, dict) else None
    if not screenshot_index and isinstance(report.get("screenshots_index"), str):
        screenshot_index = report["screenshots_index"]
    profile_value = {
        "model": str(model),
        "context_window": _number(report.get("context_window")) or (
            _number(profile.get("context_window")) if isinstance(profile, dict) else None),
    }
    return {
        "job_id": str(report.get("job_id") or "not reported"),
        "kind": str(report.get("kind") or report.get("job_kind") or "not reported"),
        "tier": str(report.get("tier") or "not reported"),
        "model": str(model),
        "profile": profile_value,
        "attempts": len(attempts),
        "review_findings_count": len(review_findings) if isinstance(review_findings, list) else None,
        "first_try_success": bool(report.get("status") in ("ok", "ready") and len(attempts) == 1
                                  and attempts[0].get("returncode") == 0
                                  and not attempts[0].get("terminal_failures")
                                  and not attempts[0].get("timed_out")
                                  and not attempts[0].get("stalled")
                                  and not attempts[0].get("interrupted")),
        "durations": {
            "worker": _finite_number(timings.get("worker_seconds")),
            "verification": _finite_number(timings.get("verification_seconds")),
            "native": _finite_number(timings.get("native_preflight_seconds")),
            "screenshots": _finite_number(timings.get("screenshots_seconds")),
            "total": _finite_number(report.get("elapsed_seconds")),
        },
        "tokens": tokens,
        "status": str(report.get("status") or "not reported"),
        "classification": {"class": classification.get("class"),
                           "code": classification.get("code"),
                           "plugin_suggestion": _clean_line(classification.get("plugin_suggestion"))},
        "escalates": str(report["escalates"]) if report.get("escalates") else None,
        "report_path": str(report.get("report_path") or
                            (str(Path(report["report_dir"]) / "report.json")
                             if report.get("report_dir") else "not reported")),
        "screenshots_index": str(screenshot_index) if screenshot_index else None,
    }


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _clean_line(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:500] or None


def append_job(state_dir: Path, session: str, report: Dict) -> Path:
    """Append one whitelist-only JSON row under an exclusive lock."""
    _validate_session(session)
    if not isinstance(report, dict):
        raise TypeError("report must be a dictionary")
    directory = Path(state_dir) / "sessions" / session
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ledger.jsonl"
    row = _safe_report(report)
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    with open(str(path), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for line_number, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise ValueError("ledger has a truncated final row at line %d" % line_number)
                try:
                    existing = json.loads(line)
                except ValueError as error:
                    raise ValueError("ledger contains invalid JSON at line %d" % line_number) from error
                if not isinstance(existing, dict):
                    raise ValueError("ledger row %d is not an object" % line_number)
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return path


def _count_fields(raw: Any) -> Dict[str, Optional[int]]:
    raw = raw if isinstance(raw, dict) else {}
    result: Dict[str, Optional[int]] = {}
    for field in COUNT_FIELDS:
        result[field] = next((_number(raw.get(key)) for key in _USAGE_KEYS[field]
                              if _number(raw.get(key)) is not None), None)
    result["total_tokens"] = _number(raw.get("total_tokens"))
    return result


def _latest_rollout_usage(path: Path, wanted_session: str) -> Tuple[
        Optional[Dict[str, Optional[int]]], List[str], str, Optional[str], Optional[str]]:
    """Read filtered usage and matching task completion evidence from a rollout."""
    session = None
    parent = None
    latest = None
    as_of = None
    active_turn_id = None
    completed_turn_id = None
    count_after_active_start = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if kind == "session_meta":
                    candidate = payload.get("id") or payload.get("session_id")
                    if isinstance(candidate, str):
                        session = candidate
                    parent = next((payload.get(key) for key in _PARENT_KEYS
                                   if isinstance(payload.get(key), str)), None)
                elif kind == "event_msg":
                    event_type = payload.get("type")
                    turn_id = payload.get("turn_id")
                    if event_type == "task_started":
                        active_turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
                        completed_turn_id = None
                        count_after_active_start = False
                    elif event_type == "task_complete":
                        if (active_turn_id is not None and isinstance(turn_id, str)
                                and turn_id == active_turn_id):
                            completed_turn_id = turn_id
                    elif event_type == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            total = info.get("total_token_usage")
                            if isinstance(total, dict):
                                latest = _count_fields(total)
                                count_after_active_start = active_turn_id is not None
                                # The timestamp belongs to this count only; never carry
                                # forward the timestamp from an older observation.
                                as_of = None
                                timestamp = event.get("timestamp")
                                if isinstance(timestamp, str) and timestamp:
                                    as_of = timestamp
    except OSError:
        return None, [], "snapshot", None, None
    if session != wanted_session:
        return None, [], "snapshot", None, None
    completeness = ("final" if active_turn_id is not None and
                    completed_turn_id == active_turn_id and
                    count_after_active_start else "snapshot")
    return latest, ([parent] if parent else []), completeness, as_of, completed_turn_id


def planner_usage(session: str, codex_home: Path) -> Optional[Dict]:
    """Return filtered planner and linked-child usage, or None when no rollout matches."""
    _validate_session(session)
    home = Path(codex_home)
    if not home.is_dir() or home.is_symlink():
        return None
    found: Dict[str, Dict[str, Optional[int]]] = {}
    parents: Dict[str, List[str]] = {}
    completeness: Dict[str, str] = {}
    as_of: Dict[str, Optional[str]] = {}
    try:
        paths = sorted(path for path in home.rglob("rollout*.jsonl")
                       if path.is_file() and not path.is_symlink())
    except OSError:
        paths = []
    for path in paths:
        # Only filtered session metadata, task lifecycle markers, and token counts are used.
        usage, parent_ids, usage_state, usage_as_of, _ = _latest_rollout_usage(path, session)
        if usage is not None:
            # A rollout only belongs to this session when its own metadata matches it. The
            # parent map is then used to account for directly linked child rollouts.
            found[session] = usage
            parents[session] = parent_ids
            completeness[session] = usage_state
            as_of[session] = usage_as_of
        else:
            # Child matching requires its own ID; scan its session metadata keys only.
            child_id, child_parent, child_usage, child_state, child_as_of = _read_child_metadata(path)
            if child_id and child_parent == session:
                if child_usage is not None:
                    found[child_id] = child_usage
                parents[child_id] = [session]
                completeness[child_id] = child_state
                as_of[child_id] = child_as_of
    if session not in found:
        return None
    own = found[session]
    children = [item for item, parent_ids in parents.items() if item != session and session in parent_ids]
    result: Dict[str, Any] = dict(own)
    for field in COUNT_FIELDS + ("total_tokens",):
        values = [found[session].get(field)] + [
            found.get(item, {}).get(field) for item in children]
        result[field] = sum(values) if values and all(value is not None for value in values) else None
    result["session"] = session
    result["subagents"] = ("not linked" if not children else
                           {"count": len(children), "sessions": sorted(children)})
    incomplete_children = sorted(item for item in children
                                 if completeness.get(item) != "final" or item not in found)
    result["completeness"] = ("final" if completeness.get(session) == "final" and
                              not incomplete_children else "snapshot")
    result["source"] = "codex_rollout_token_count"
    result["as_of"] = as_of.get(session)
    result["incomplete_children"] = incomplete_children
    return result


def _read_child_metadata(path: Path) -> Tuple[
        Optional[str], Optional[str], Optional[Dict[str, Optional[int]]], str, Optional[str]]:
    child_id = parent_id = None
    latest = None
    as_of = None
    active_turn_id = None
    completed_turn_id = None
    count_after_active_start = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                    continue
                payload = event["payload"]
                if event.get("type") == "session_meta":
                    child_id = payload.get("id") or payload.get("session_id")
                    parent_id = next((payload.get(key) for key in _PARENT_KEYS
                                      if isinstance(payload.get(key), str)), None)
                elif event.get("type") == "event_msg":
                    event_type = payload.get("type")
                    turn_id = payload.get("turn_id")
                    if event_type == "task_started":
                        active_turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
                        completed_turn_id = None
                        count_after_active_start = False
                    elif event_type == "task_complete":
                        if (active_turn_id is not None and isinstance(turn_id, str)
                                and turn_id == active_turn_id):
                            completed_turn_id = turn_id
                    elif event_type == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                            latest = _count_fields(info["total_token_usage"])
                            count_after_active_start = active_turn_id is not None
                            as_of = None
                            timestamp = event.get("timestamp")
                            if isinstance(timestamp, str) and timestamp:
                                as_of = timestamp
    except OSError:
        return None, None, None, "snapshot", None
    state = ("final" if active_turn_id is not None and
             completed_turn_id == active_turn_id and
             count_after_active_start else "snapshot")
    return ((child_id, parent_id, latest, state, as_of) if isinstance(child_id, str)
            else (None, None, None, "snapshot", None))


def _read_ledger(path: Path, *, tolerate_corruption: bool = False) -> Tuple[List[Dict[str, Any]], List[str]]:
    rows = []
    problems = []
    if not path.exists():
        return rows, problems
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                problem = "truncated final row at line %d" % line_number
                if not tolerate_corruption:
                    raise ValueError("ledger has a " + problem)
                problems.append(problem)
                break
            try:
                value = json.loads(line)
            except ValueError:
                problem = "invalid JSON at line %d" % line_number
                if not tolerate_corruption:
                    raise ValueError("ledger contains " + problem)
                problems.append(problem)
                continue
            if not isinstance(value, dict):
                problem = "row %d is not an object" % line_number
                if not tolerate_corruption:
                    raise ValueError("ledger " + problem)
                problems.append(problem)
                continue
            rows.append(value)
    return rows, problems


def _totals(rows: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    # Explicit zero-attempt jobs never invoked a worker. Unknown legacy rows still count.
    rows = [row for row in rows if row.get("attempts") != 0]
    result = {}
    for field in COUNT_FIELDS:
        # Ledger token names are concise but remain explicit in the public summary.
        field_key = {"input_tokens": "input", "cached_input_tokens": "cached",
                     "output_tokens": "output", "reasoning_tokens": "reasoning"}[field]
        values = [row.get("tokens", {}).get(field_key) for row in rows]
        result[field] = sum(values) if values and all(type(value) is int for value in values) else None
    return result


def build_summary(state_dir: Path, session: str, codex_home: Path) -> Dict:
    _validate_session(session)
    rows, ledger_problems = _read_ledger(Path(state_dir) / "sessions" / session / "ledger.jsonl",
                                         tolerate_corruption=True)
    tiers: Dict[str, List[Dict[str, Any]]] = {}
    models: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        tiers.setdefault(str(row.get("tier") or "not reported"), []).append(row)
        models.setdefault(str(row.get("model") or "not reported"), []).append(row)
    planner = planner_usage(session, codex_home)
    totals = {tier: _totals(items) for tier, items in sorted(tiers.items())}
    by_model = {model: _totals(items) for model, items in sorted(models.items())}
    job_totals = _totals(rows)
    if ledger_problems:
        # A partial ledger cannot support exact aggregates. Keep known rows visible, while
        # withholding totals that could otherwise be mistaken for complete session usage.
        for values in list(totals.values()) + list(by_model.values()) + [job_totals]:
            for key in values:
                values[key] = None
    all_input_parts = [job_totals.get("input_tokens")]
    all_output_parts = [job_totals.get("output_tokens")]
    if planner is not None and planner.get("completeness") == "final":
        all_input_parts.append(planner.get("input_tokens"))
        all_output_parts.append(planner.get("output_tokens"))
    all_input = sum(all_input_parts) if all_input_parts and all(
        value is not None for value in all_input_parts) else None
    all_output = sum(all_output_parts) if all_output_parts and all(
        value is not None for value in all_output_parts) else None
    local_input = totals.get("local", {}).get("input_tokens")
    local_output = totals.get("local", {}).get("output_tokens")
    local_tokens = local_input + local_output if local_input is not None and local_output is not None else None
    denominator = all_input + all_output if all_input is not None and all_output is not None else None
    local_share = (local_tokens / denominator if local_tokens is not None and denominator and
                   planner is not None and planner.get("completeness") == "final" else None)
    suggestions = sorted({suggestion for row in rows
                          if (suggestion := row.get("classification", {}).get("plugin_suggestion"))})
    permissions_log = Path(state_dir) / "permissions.log"
    permission_actions = set()
    if permissions_log.is_file() and not permissions_log.is_symlink():
        try:
            for line in permissions_log.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("action"), str):
                    permission_actions.add(event["action"])
        except OSError:
            pass
    if permission_actions:
        suggestions.append("Review global permissions changes recorded in launcher state.")
    escalations = []
    for row in rows:
        if row.get("escalates"):
            parent = next((candidate for candidate in rows
                           if candidate.get("job_id") == row.get("escalates")), {})
            reason = parent.get("classification", {}).get("code") or parent.get("status") or "not reported"
            escalations.append({"from": row.get("escalates"), "to": row.get("job_id"), "reason": reason})
    screenshots = [{"job_id": row.get("job_id"), "index": row.get("screenshots_index")}
                   for row in rows if row.get("screenshots_index")]
    success_rows = [row for row in rows if row.get("status") in ("ok", "ready")]
    first_try = [row for row in rows if row.get("attempts", 0) > 0]
    return {
        "session": session,
        "jobs": rows,
        "totals": totals,
        "totals_by_model": by_model,
        "all_job_totals": job_totals,
        "ledger_integrity": "partial" if ledger_problems else "complete",
        "ledger_issues": ledger_problems,
        "planner": planner,
        "planner_linkage": "Unlinked subagent usage may be absent; only rollouts whose session "
                           "metadata links them to this planner are included.",
        "local_share": local_share,
        "first_try_success_rate": (sum(bool(row.get("first_try_success")) for row in first_try) /
                                    len(first_try) if first_try else None),
        "success_count": len(success_rows),
        "escalations": escalations,
        "screenshots": screenshots,
        "suggested_plugin_fixes": sorted(set(suggestions)),
    }


def _format(value: Any) -> str:
    return "not reported" if value is None else str(value)


def render_markdown(summary: Dict) -> str:
    lines = ["# Local Worker session summary", "", "Session: `%s`" % summary.get("session", "not reported"), "",
             "## Jobs", "", "| Job | Kind | Tier | Model | Attempts | Status | Advisory test findings | Input | Cached | Output | Reasoning |",
             "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in summary.get("jobs", []):
        tokens = row.get("tokens", {})
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            row.get("job_id", "not reported"), row.get("kind", "not reported"),
            row.get("tier", "not reported"), row.get("model", "not reported"),
            _format(row.get("attempts")), row.get("status", "not reported"),
            _format(row.get("review_findings_count")),
            _format(tokens.get("input")), _format(tokens.get("cached")),
            _format(tokens.get("output")), _format(tokens.get("reasoning"))))
    lines += ["", "## Totals by tier", "", "| Tier | Input | Cached | Output | Reasoning |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for tier, counts in summary.get("totals", {}).items():
        lines.append("| %s | %s | %s | %s | %s |" % (tier,
            _format(counts.get("input_tokens")), _format(counts.get("cached_input_tokens")),
            _format(counts.get("output_tokens")), _format(counts.get("reasoning_tokens"))))
    lines += ["", "## Totals by model", "", "| Model | Input | Cached | Output | Reasoning |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for model, counts in summary.get("totals_by_model", {}).items():
        lines.append("| %s | %s | %s | %s | %s |" % (model,
            _format(counts.get("input_tokens")), _format(counts.get("cached_input_tokens")),
            _format(counts.get("output_tokens")), _format(counts.get("reasoning_tokens"))))
    lines += ["", "## Planner tokens", ""]
    planner = summary.get("planner")
    if planner is None:
        lines.append("Planner usage: not reported.")
    else:
        completeness = planner.get("completeness", "snapshot")
        lines.append("Planner usage source: %s; completeness: %s." % (
            planner.get("source", "not reported"), completeness))
        if planner.get("as_of"):
            lines.append("Latest planner usage snapshot: %s." % planner["as_of"])
        if completeness != "final":
            lines.append("Planner usage is an incomplete rollout snapshot and may omit usage "
                         "after this summary ran; rerun summary after the Codex turn completes.")
        if planner.get("incomplete_children"):
            lines.append("Linked child usage is missing or active for: %s." %
                         ", ".join(planner["incomplete_children"]))
        lines.append("Input: %s; cached input: %s; output: %s; reasoning: %s; total: %s; subagents: %s." % (
            _format(planner.get("input_tokens")), _format(planner.get("cached_input_tokens")),
            _format(planner.get("output_tokens")), _format(planner.get("reasoning_tokens")),
            _format(planner.get("total_tokens")), planner.get("subagents", "not linked")))
    lines.append(summary.get("planner_linkage", "Unlinked subagent usage may be absent."))
    lines += ["", "Local share of job plus planner input/output tokens: %s." % (
        "not reported" if summary.get("local_share") is None else "%.1f%%" %
        (summary["local_share"] * 100)), "First-try success rate: %s." % (
            "not reported" if summary.get("first_try_success_rate") is None else
            "%.1f%%" % (summary["first_try_success_rate"] * 100)), "",
        "## Escalations", ""]
    if summary.get("escalations"):
        for escalation in summary["escalations"]:
            lines.append("- %s → %s: %s" % (escalation["from"], escalation["to"], escalation["reason"]))
    else:
        lines.append("None.")
    lines += ["", "## Screenshots", ""]
    if summary.get("screenshots"):
        for item in summary["screenshots"]:
            lines.append("- %s: %s" % (item["job_id"], item["index"]))
    else:
        lines.append("None.")
    lines += ["", "## Suggested fixes for this plugin", ""]
    if summary.get("suggested_plugin_fixes"):
        lines.extend("- " + item for item in summary["suggested_plugin_fixes"])
    else:
        lines.append("None recorded.")
    lines.append("")
    return "\n".join(lines)


def write_summary(state_dir: Path, session: str, codex_home: Path) -> Tuple[Path, Path]:
    _validate_session(session)
    summary = build_summary(state_dir, session, codex_home)
    directory = Path(state_dir) / "sessions" / session
    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / "summary.md"
    json_path = directory / "summary.json"
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path, json_path
