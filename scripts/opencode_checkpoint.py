#!/usr/bin/env python3
"""Manually advanced four-phase checkpoint helper for the native OpenCode CLI.

This is an addon-free launcher: it shells out to ``opencode run`` with an explicit
``--agent``/``--model`` pair for one checkpoint phase, records complete raw evidence,
and returns failure when the model run did not produce usable output. It never edits
global model/config/provider state, never autoapproves permissions, and never chains
phases together: the caller advances discover -> plan -> implement -> review by hand and
supplies/verifies the packet and the acceptance checks between phases.

Standard library only, Python 3.9+. Importing this module has no side effects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

PHASES = ("discover", "plan", "implement", "review")

# phase -> (native OpenCode agent, model tier). Routing is a fixed table lookup, so the
# same arguments always produce the same command and nothing is read from or written to
# global OpenCode configuration.
ROUTING = {
    "discover": ("plan", "local"),
    "plan": ("plan", "cloud"),
    "implement": ("build", "local"),
    "review": ("plan", "cloud"),
}

PHASE_INSTRUCTIONS = {
    "discover": (
        "Phase: discover (read-only).\n"
        "Gather only evidence relevant to the packet below: file paths, symbols, current"
        " behaviour, existing tests and the commands that run them. Read; do not create,"
        " edit or delete any file. Do not delegate to subagents or other models. Cite every"
        " finding with a repository-relative path (and line range when useful) and state"
        " what is still unknown."
    ),
    "plan": (
        "Phase: plan (no edits).\n"
        "From the packet and the evidence above, write a short bounded implementation"
        " contract: the exact files/paths that may change, the ordered edits, the ordinary"
        " and trusted test commands to run, and the retry budget. Preserve every acceptance"
        " criterion from the packet verbatim; do not widen scope or invent requirements."
        " Do not create, edit or delete files. Do not delegate to subagents or other models."
        " End with exactly one final line: ``PLAN: READY`` or ``PLAN: BLOCKED: <reason>``."
    ),
    "implement": (
        "Phase: implement (allowed paths only).\n"
        "Implement the contract above and change only the assigned allowed paths listed in"
        " the packet. Run the packet's ordinary, trusted test commands and repair your own"
        " failures within the packet retry budget; never weaken, skip or delete a test to"
        " make it pass. Do not delegate to subagents or other models. Do not commit, merge,"
        " push or create branches. Preserve failure evidence: include the failing command"
        " and its real output in your final message, and state every path you changed."
    ),
    "review": (
        "Phase: review (read-only, semantic).\n"
        "Review the complete supplied patch, surrounding source and tests. Read; do not"
        " create, edit or delete files. Do not delegate to subagents or other models."
        " Judge whether the change actually satisfies each acceptance criterion in the"
        " packet, including edge cases, error paths and scope. Passing tests alone are not"
        " sufficient evidence: say which criterion each piece of evidence satisfies. End"
        " with exactly one final line: ``VERDICT: PASS`` or ``VERDICT: CHANGES_REQUIRED``."
    ),
}

# Bounded escalation: SIGTERM the whole group, then SIGKILL, then reap.
DRAIN_POLL_SECONDS = 0.05
TERMINATE_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 5.0
EXIT_GRACE_SECONDS = 5.0
READ_CHUNK_BYTES = 65536
ERROR_SUMMARY_CHARS = 500

# Accurate secrets statement: no credential is read or dumped, but the exact packet text is
# persisted verbatim, so keeping secrets out is the caller's responsibility.
SECRETS_NOTE = (
    "no credential is read from the environment, forwarded or printed, and the inherited "
    "OpenCode/auth environment is never dumped into the evidence; however the exact packet text "
    "is persisted verbatim in prompt.txt and recorded in argv, so keep secrets out of the packet "
    "and the command line"
)

# Envelope types whose own ``error``/``properties.error`` field is a real provider/session
# failure. Anything else (tool inputs, tool outputs, tool metadata) is data, never a fatal error.
ERROR_ENVELOPE_TYPES = ("message.updated", "message.part.updated", "session.error")


class CheckpointError(Exception):
    """A caller-visible mistake detected before or while launching a checkpoint."""


def validate_cloud_model(value):
    """Require an explicit ``provider/model#variant`` and nothing looser."""
    text = value or ""
    if any(char.isspace() for char in text):
        raise CheckpointError("--cloud-model must not contain whitespace")
    parts = text.split("#")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CheckpointError(
            "--cloud-model must be an explicit provider/model#variant, got %r" % (value,))
    provider, _, model = parts[0].partition("/")
    if not provider or not model or "/" in model:
        raise CheckpointError(
            "--cloud-model must be an explicit provider/model#variant, got %r" % (value,))
    return text


def validate_local_model(value):
    """Require an explicit ``provider/model`` with no variant suffix."""
    text = value or ""
    if any(char.isspace() for char in text):
        raise CheckpointError("--local-model must not contain whitespace")
    if "#" in text:
        raise CheckpointError("--local-model must be provider/model without #variant")
    provider, _, model = text.partition("/")
    if not provider or not model or "/" in model:
        raise CheckpointError("--local-model must be an explicit provider/model, got %r" % (value,))
    return text


def validate_repo(value):
    """Absolute, existing Git checkout; nothing else is inspected or mutated."""
    path = Path(value)
    if not path.is_absolute():
        raise CheckpointError("--repo must be an absolute path, got %r" % (value,))
    if not path.is_dir():
        raise CheckpointError("--repo is not an existing directory: %s" % (path,))
    if not (path / ".git").exists():
        raise CheckpointError("--repo is not a Git checkout (no .git entry): %s" % (path,))
    return path


def read_packet(path):
    """Read the existing UTF-8 packet, or explain why it cannot be used."""
    packet = Path(path)
    if not packet.is_file():
        raise CheckpointError("--packet is not an existing file: %s" % (packet,))
    try:
        return packet.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CheckpointError("--packet is not valid UTF-8: %s (%s)" % (packet, exc))


def routing_for(phase, cloud_model, local_model):
    """Deterministic phase -> (agent, model) table lookup."""
    if phase not in PHASES:
        raise CheckpointError("unknown phase %r (choose one of %s)" % (phase, ", ".join(PHASES)))
    agent, tier = ROUTING[phase]
    return agent, (cloud_model if tier == "cloud" else local_model), tier


def build_prompt(phase, packet_path, packet_text):
    """Exact prompt text: phase instructions plus the full packet."""
    return (
        PHASE_INSTRUCTIONS[phase]
        + "\n\n---\nPACKET FILE: "
        + str(packet_path)
        + "\n---\n"
        + packet_text.rstrip("\n")
        + "\n"
    )


def build_argv(opencode, agent, model, prompt):
    """A NEW native OpenCode session per stage: standalone, JSON events, explicit model."""
    return [opencode, "run", "--standalone", "--format", "json",
            "--agent", agent, "--model", model, prompt]


def dry_run_report(options, agent, model, tier, prompt, evidence):
    """Routing metadata for --dry-run; never dumps packet/prompt contents."""
    argv = build_argv(options.opencode, agent, model, prompt)
    shown = list(argv[:-1]) + ["<PROMPT %d bytes: phase instructions + full packet, not printed>"
                               % len(prompt.encode("utf-8"))]
    prompt_bytes = len(prompt.encode("utf-8"))
    return {
        "dry_run": True,
        "model_calls": 0,
        "filesystem_writes": 0,
        "phase": options.phase,
        "agent": agent,
        "model": model,
        "model_tier": tier,
        "cloud_model": options.cloud_model,
        "local_model": options.local_model,
        "cwd_pinned": str(options.repo),
        "pwd_pinned": str(options.repo),
        "timeout_seconds": options.timeout,
        "evidence_dir": str(evidence),
        "packet_path": str(options.packet),
        "packet_bytes": prompt_bytes - len(PHASE_INSTRUCTIONS[options.phase].encode("utf-8")),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "argv": shown,
        "argv_secrets": SECRETS_NOTE,
        "session_new": True,
        "shell": False,
        "autoapprove": False,
    }


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            for entry in _iter_dicts(item):
                yield entry
    elif isinstance(value, (list, tuple)):
        for item in value:
            for entry in _iter_dicts(item):
                yield entry


def extract_text_parts(event):
    """Assistant text from the native OpenCode CLI text envelopes only.

    Exactly two documented shapes count as assistant text:

    * ``{"type": "text", "part": {"type": "text", "text": "..."}}`` -- the native CLI
      message part emitted on stdout by ``opencode run --format json``;
    * ``{"type": "message.part.updated", "properties": {"part": {"type": "text",
      "text": "..."}}}`` -- the same part carried inside a bus event.

    Nothing is searched for recursively on purpose: a ``tool_use`` event legitimately
    carries ``{"type": "text", "text": "..."}`` blocks inside ``part.state.input``,
    ``part.state.output`` or ``part.state.metadata.content``, and that is tool data, not
    an assistant reply. Treating it as response text would fabricate a successful answer
    (and hide a ``no_assistant_text`` failure) from a run that produced none.
    """
    if not isinstance(event, dict):
        return []
    event_type = event.get("type")
    if event_type == "text":
        candidates = [event.get("part")]
    elif event_type == "message.part.updated":
        properties = event.get("properties")
        candidates = [properties.get("part")] if isinstance(properties, dict) else []
    else:
        return []
    parts = []
    for part in candidates:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if not isinstance(text, str) and event_type == "text" \
                    and isinstance(event.get("text"), str):
                text = event["text"]
            if isinstance(text, str):
                parts.append(text)
    return parts


def extract_session_ids(event):
    ids = []
    for node in _iter_dicts(event):
        for key in ("sessionID", "session_id", "sessionUuid", "session"):
            value = node.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
    return ids


def _summarize(value):
    if isinstance(value, str):
        return value[:ERROR_SUMMARY_CHARS]
    try:
        text = json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return text[:ERROR_SUMMARY_CHARS]


def extract_error(event):
    """A short summary for a real provider/session error envelope, else ``None``.

    Only documented failure envelopes count: a top-level ``{"type": "error", ...}`` event,
    or an error carried by a message/session envelope
    (``message.updated``/``message.part.updated``/``session.error``) in its ``error`` or
    ``properties.error`` field. Tool results are never scanned for ``error`` keys: a
    completed ``tool_use`` whose ``state.metadata`` or output payload happens to contain
    ``error`` (a non-zero shell payload, a JSON body with an ``error`` field) is data the
    assistant consumed, not a fatal provider failure.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") == "error":
        return _summarize(event.get("error") or event)
    if event.get("type") in ERROR_ENVELOPE_TYPES:
        if event.get("error"):
            return _summarize(event["error"])
        properties = event.get("properties")
        if isinstance(properties, dict) and properties.get("error"):
            return _summarize(properties["error"])
    return None


def parse_events(path):
    """Read raw events.jsonl back: counts, assistant text, session ids, error summaries."""
    lines = parsed = unparsed = 0
    text_parts = []
    session_ids = []
    errors = []
    if path.exists():
        raw = path.read_text(encoding="utf-8", errors="replace")
        for line in raw.splitlines():
            if not line.strip():
                continue
            lines += 1
            try:
                event = json.loads(line)
            except ValueError:
                unparsed += 1
                continue
            parsed += 1
            text_parts.extend(extract_text_parts(event))
            for identifier in extract_session_ids(event):
                if identifier not in session_ids:
                    session_ids.append(identifier)
            error = extract_error(event)
            if error is not None:
                errors.append(error)
    return {
        "event_lines": lines,
        "parsed_events": parsed,
        "unparsed_event_lines": unparsed,
        "assistant_text": "".join(text_parts),
        "session_ids": session_ids,
        "error_events": errors,
    }


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(process, terminate_grace=TERMINATE_GRACE_SECONDS,
                            kill_grace=KILL_GRACE_SECONDS):
    """Terminate the child's whole process group, escalate to kill if needed, then reap."""
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None
    for sig, grace in ((signal.SIGTERM, terminate_grace), (signal.SIGKILL, kill_grace)):
        try:
            if pgid is None:
                process.send_signal(sig)
            else:
                os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except OSError:
            if pgid is None:
                break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    if pgid is not None and _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        return process.wait(timeout=kill_grace)
    except subprocess.TimeoutExpired:
        return process.poll()


def drain_pipes(process, events_path, stderr_path, deadline):
    """Stream child stdout/stderr to evidence as it arrives; return whether we timed out."""
    selector = selectors.DefaultSelector()
    events_file = open(events_path, "wb")
    stderr_file = open(stderr_path, "wb")
    pipes = []
    timed_out = False
    try:
        for pipe, sink in ((process.stdout, events_file), (process.stderr, stderr_file)):
            if pipe is not None:
                selector.register(pipe, selectors.EVENT_READ, sink)
                pipes.append(pipe)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _mask in selector.select(timeout=min(remaining, DRAIN_POLL_SECONDS)):
                chunk = key.fileobj.read1(READ_CHUNK_BYTES)
                if chunk:
                    key.data.write(chunk)
                    key.data.flush()
                else:
                    selector.unregister(key.fileobj)
    finally:
        selector.close()
        for pipe in pipes:
            try:
                pipe.close()
            except OSError:
                pass
        events_file.close()
        stderr_file.close()
    return timed_out


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_fresh_output(path):
    """Create the evidence directory with exist_ok=False; refuse anything already there."""
    output = Path(path)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise CheckpointError(
            "--output already exists; refusing to reuse or overwrite evidence: %s" % (output,))
    except OSError as exc:
        raise CheckpointError("cannot create --output %s: %s" % (output, exc))
    return output


def run_checkpoint(options):
    """Execute one checkpoint phase, save complete evidence, return the helper status."""
    agent, model, tier = routing_for(options.phase, options.cloud_model, options.local_model)
    packet_text = read_packet(options.packet)
    prompt = build_prompt(options.phase, options.packet, packet_text)
    argv = build_argv(options.opencode, agent, model, prompt)

    if options.dry_run:
        if Path(options.output).exists():
            raise CheckpointError(
                "--output already exists; refusing to reuse or overwrite evidence: %s"
                % (options.output,))
        report = dry_run_report(options, agent, model, tier, prompt, Path(options.output))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    evidence = ensure_fresh_output(options.output)
    events_path = evidence / "events.jsonl"
    stderr_path = evidence / "stderr.log"
    prompt_path = evidence / "prompt.txt"
    response_path = evidence / "response.txt"
    execution_path = evidence / "execution.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    events_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    response_path.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PWD"] = str(options.repo)

    started_at = _utc_now()
    started = time.monotonic()
    exit_code = None
    timed_out = False
    launch_error = None
    process = None
    try:
        process = subprocess.Popen(argv, cwd=str(options.repo), env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        launch_error = "%s: %s" % (type(exc).__name__, exc)

    if process is not None:
        deadline = started + options.timeout
        timed_out = drain_pipes(process, events_path, stderr_path, deadline)
        if timed_out:
            exit_code = terminate_process_group(process)
        else:
            remaining = max(0.0, deadline - time.monotonic()) + EXIT_GRACE_SECONDS
            try:
                exit_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = terminate_process_group(process)
    else:
        stderr_path.write_bytes((launch_error + "\n").encode("utf-8"))

    elapsed = time.monotonic() - started
    summary = parse_events(events_path)
    text = summary["assistant_text"]
    response_path.write_text(text, encoding="utf-8")

    reasons = []
    if launch_error is not None:
        reasons.append("launch_failed")
    if timed_out:
        reasons.append("timeout")
    if launch_error is None and exit_code != 0:
        reasons.append("nonzero_exit")
    if summary["error_events"]:
        reasons.append("error_event")
    if not text.strip():
        reasons.append("no_assistant_text")

    record = {
        "helper": "opencode_checkpoint",
        "dry_run": False,
        "phase": options.phase,
        "agent": agent,
        "model": model,
        "model_tier": tier,
        "cloud_model": options.cloud_model,
        "local_model": options.local_model,
        "session_new": True,
        "shell": False,
        "autoapprove": False,
        "repo": str(options.repo),
        "cwd_pinned": str(options.repo),
        "pwd_pinned": str(options.repo),
        "packet": str(options.packet),
        "packet_bytes": len(packet_text.encode("utf-8")),
        "argv": argv,
        "argv_secrets": SECRETS_NOTE,
        "opencode_executable": options.opencode,
        "timeout_seconds": options.timeout,
        "launched": process is not None,
        "launch_error": launch_error,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "elapsed_seconds": round(elapsed, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "session_ids": summary["session_ids"],
        "event_lines": summary["event_lines"],
        "parsed_events": summary["parsed_events"],
        "unparsed_event_lines": summary["unparsed_event_lines"],
        "error_events": summary["error_events"],
        "assistant_text_bytes": len(text.encode("utf-8")),
        "response_present": bool(text.strip()),
        "failure_reasons": reasons,
        "evidence_dir": str(evidence),
        "evidence_files": ["prompt.txt", "events.jsonl", "stderr.log", "response.txt",
                           "execution.json"],
    }
    execution_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
    print(json.dumps({"phase": options.phase, "agent": agent, "model": model,
                      "evidence_dir": str(evidence), "exit_code": exit_code,
                      "timed_out": timed_out, "failure_reasons": reasons,
                      "session_ids": summary["session_ids"]}, sort_keys=True))
    return 1 if reasons else 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="opencode_checkpoint.py",
        description="Manually advanced four-phase checkpoint helper for the native OpenCode "
                    "CLI: one fresh session per phase, explicit agent/model, complete raw "
                    "evidence, no global configuration changes, no permission autoapproval.")
    parser.add_argument("--phase", required=True, choices=list(PHASES))
    parser.add_argument("--repo", required=True,
                        help="absolute path to an existing Git checkout (pinned as cwd and PWD)")
    parser.add_argument("--packet", required=True, help="existing UTF-8 task packet file")
    parser.add_argument("--output", required=True,
                        help="fresh evidence directory; an existing path is refused")
    parser.add_argument("--cloud-model", dest="cloud_model", required=True,
                        help="explicit provider/model#variant used for plan and review")
    parser.add_argument("--local-model", dest="local_model", required=True,
                        help="explicit provider/model used for discover and implement")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds; must be a finite number greater than zero")
    parser.add_argument("--opencode", default="opencode",
                        help="opencode executable (default: opencode)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print effective routing/command metadata; no model, no writes")
    return parser


def prepare(options):
    """Validate caller input before anything is created or launched."""
    if not options.repo or not options.packet or not options.output:
        raise CheckpointError("--repo, --packet and --output are required")
    if not math.isfinite(options.timeout):
        # inf/nan would make the drain deadline useless (never fires, or fires instantly).
        raise CheckpointError("--timeout must be a finite number of seconds, got %r"
                              % (options.timeout,))
    if options.timeout <= 0:
        raise CheckpointError("--timeout must be greater than zero")
    options.repo = validate_repo(options.repo)
    options.packet = Path(options.packet)
    options.output = Path(options.output)
    options.cloud_model = validate_cloud_model(options.cloud_model)
    options.local_model = validate_local_model(options.local_model)
    return options


def main(argv=None):
    options = build_parser().parse_args(argv)
    try:
        return run_checkpoint(prepare(options))
    except CheckpointError as exc:
        sys.stderr.write("opencode_checkpoint: %s\n" % (exc,))
        return 2


if __name__ == "__main__":
    sys.exit(main())
