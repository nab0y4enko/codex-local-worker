"""Bounded streaming subprocesses and factual, redacted local-worker evidence.

Only documented event locations carry usage. Quoted source, command output and
arbitrary recursively nested keys are deliberately never usage evidence.
"""
from __future__ import annotations

import codecs
import contextlib
from datetime import datetime, timezone
import json
import os
import process_supervisor
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")
# A worker or a test command can flood its pipes. Memory stays bounded by retaining at
# most this many bytes per stream; the caller treats the truncation as a failure.
CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
TOOLS = {"command_execution", "function_call", "exec_command", "mcp_tool_call", "web_search",
        "file_change"}
# Documented top-level events that end a run unsuccessfully, whatever the exit code claims.
TERMINAL_EVENT_TYPES = ("turn.failed", "response.failed", "error")
# The two documented item lifecycle stages: separate counts that are never merged.
TOOL_LIFECYCLE = ("item.started", "item.completed")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _counts(value):
    if not isinstance(value, dict):
        return dict.fromkeys(FIELDS)
    details = value.get("input_tokens_details")
    cached = value.get("cached_input_tokens")
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return {"input_tokens": _number(value.get("input_tokens", value.get("prompt_tokens"))),
            "output_tokens": _number(value.get("output_tokens", value.get("completion_tokens"))),
            "cached_input_tokens": _number(cached)}


def _result(records, sources, *, snapshots=False):
    counts = {name: (sum(row[name] for row in records if row[name] is not None)
                     if any(row[name] is not None for row in records) else None) for name in FIELDS}
    complete = bool(records) and all(row[name] is not None for row in records for name in FIELDS[:2])
    available = any(counts[name] is not None for name in FIELDS)
    usage = dict(counts, source=("cumulative_snapshot" if snapshots else
                                (next(iter(sources)) if len(sources) == 1 else
                                 "mixed" if sources else None)),
                 sources=sorted(sources), availability=("complete" if complete else
                                                       "partial" if available else "unavailable"),
                 complete=complete, observed_records=len(records),
                 started_at=None, finished_at=None, elapsed_seconds=None,
                 coverage={"records": len(records), "field_observations": {
                     name: sum(row[name] is not None for row in records) for name in FIELDS}})
    return {"token_counts": {name: counts[name] for name in FIELDS[:2]}, "usage": usage}


class _UsageAccumulator:
    def __init__(self):
        self.records = []
        self.sources = set()
        self.seen = set()
        self.snapshot = None

    def add(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        payload = event
        if kind == "event_msg":
            payload = event.get("payload", event.get("item"))
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                return
            kind = "token_count"
        info = payload.get("info") if isinstance(payload, dict) else None
        if kind == "token_count" and isinstance(info, dict):
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                self.snapshot = _counts(total)
            return
        if kind not in {"turn.completed", "response.completed", "token_usage_record", "token_count"}:
            return
        if kind == "response.completed":
            payload = event.get("response")
            if not isinstance(payload, dict):
                return
        elif kind == "token_usage_record" and "payload" in event:
            payload = event["payload"]
            if not isinstance(payload, dict):
                return
        raw = payload.get("usage", payload.get("token_usage"))
        if raw is None and kind == "token_usage_record":
            raw = payload
        if not isinstance(raw, dict):
            return
        identity = payload.get("response_id") or event.get("response_id") or payload.get("turn_id") or event.get("turn_id") or payload.get("id") or event.get("id")
        if isinstance(identity, (str, int)):
            # Record identity is shared across response.completed and usage_record.
            if identity in self.seen:
                return
            self.seen.add(identity)
        self.records.append(_counts(raw))
        self.sources.add(kind)

    def result(self):
        if self.records:
            return _result(self.records, self.sources)
        if self.snapshot is not None:
            return _result([self.snapshot], {"event_msg/token_count"}, snapshots=True)
        return _result([], set())


_UNSUPPORTED_RE = re.compile(r"unsupported (?:tool|function)|tool .{0,40}not (?:supported|available)", re.I)
_DIAGNOSTIC_RE = re.compile(r"\b(?:error|errors|fatal|panic|exception|traceback|failed|failure|"
                            r"warning|refused|rejected|denied)\b", re.I)
_ERROR_EVENT_TYPES = ("error", "tool_error", "tool_call_error", "function_call_error")
_FAILURE_STATUSES = ("failed", "failure", "error")
_FAILURE_STATUS_KEYS = ("status", "exit_status", "state", "outcome")
_DIAGNOSTIC_KEYS = ("error", "message", "detail", "details", "reason", "msg")
_PAYLOAD_KEYS = ("output", "content", "result", "text")


def _walk_values(node, key):
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            for found in _walk_values(value, key):
                yield found
    elif isinstance(node, list):
        for item in node:
            for found in _walk_values(item, key):
                yield found


def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def _failure_texts(event):
    for node in _walk_dicts(event):
        kind = node.get("type")
        kind = kind.lower() if isinstance(kind, str) else ""
        declared = (kind in _ERROR_EVENT_TYPES or node.get("error") not in (None, False, "", {}, [], 0)
                    or node.get("is_error") is True or node.get("ok") is False
                    or node.get("success") is False)
        failed = declared or any(isinstance(node.get(name), str)
                                 and node[name].lower() in _FAILURE_STATUSES
                                 for name in _FAILURE_STATUS_KEYS)
        if failed:
            keys = list(_DIAGNOSTIC_KEYS)
            if declared:
                keys.extend(_PAYLOAD_KEYS)
            for key in keys:
                yield from _walk_strings(node.get(key))


class EventAccumulator:
    """One incremental parser shared by live progress and post-run evidence."""
    def __init__(self):
        self.models = []
        self.started = set()
        self.completed = set()
        self.stageless = set()
        self.anonymous = 0
        self.anonymous_started = 0
        self.anonymous_completed = 0
        self.file_change_ids = set()
        self.file_change_anonymous = 0
        self.thread_id = None
        self.terminal = []
        self.unsupported = False
        self.tokens = {"input_tokens": None, "output_tokens": None}
        self.usage = _UsageAccumulator()

    def add_raw_line(self, line):
        line = line.strip()
        event = None
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                pass
        if event is None:
            if line and _UNSUPPORTED_RE.search(line) and _DIAGNOSTIC_RE.search(line):
                self.unsupported = True
            return
        self.add(event)

    def add(self, event):
        if not isinstance(event, dict):
            return
        self.usage.add(event)
        for model in event_models(event):
            if model not in self.models:
                self.models.append(model)
        for value in list(_walk_values(event, "input_tokens")) + list(_walk_values(event, "prompt_tokens")):
            if isinstance(value, int):
                self.tokens["input_tokens"] = value
        for value in list(_walk_values(event, "output_tokens")) + list(_walk_values(event, "completion_tokens")):
            if isinstance(value, int):
                self.tokens["output_tokens"] = value
        detected = tool_event(event)
        if detected is not None:
            stage, key = detected
            if key is None:
                self.anonymous += 1
                if stage == "started":
                    self.anonymous_started += 1
                elif stage == "completed":
                    self.anonymous_completed += 1
                if event.get("type") == "file_change" or (isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "file_change"):
                    self.file_change_anonymous += 1
            elif stage == "started":
                self.started.add(key)
            elif stage == "completed":
                self.completed.add(key)
            else:
                self.stageless.add(key)
            if event.get("type") == "file_change" or (isinstance(event.get("item"), dict)
                    and event["item"].get("type") == "file_change"):
                self.file_change_ids.add(key) if key is not None else None
        thread = event_thread_id(event)
        if thread and self.thread_id is None:
            self.thread_id = thread
        failure = terminal_failure(event)
        if failure and failure not in self.terminal:
            self.terminal.append(failure)
        if any(_UNSUPPORTED_RE.search(text) for text in _failure_texts(event)):
            self.unsupported = True

    def result(self):
        return {
            "observed_models": sorted(self.models), "thread_id": self.thread_id,
            "tool_call_events": len(self.started | self.completed | self.stageless) + self.anonymous,
            "tool_started_count": len(self.started) + self.anonymous_started,
            "tool_completed_count": len(self.completed) + self.anonymous_completed,
            "unsupported_tools": self.unsupported,
            "terminal_failure": bool(self.terminal),
            "terminal_failures": sorted(self.terminal), "file_changes": len(self.file_change_ids) + self.file_change_anonymous,
            **self.usage.result(),
        }


def parse_usage(text):
    """Sum typed per-response usage; cumulative snapshots are a nonadditive fallback."""
    accumulator = _UsageAccumulator()
    for line in text.splitlines():
        try:
            accumulator.add(json.loads(line))
        except (ValueError, TypeError):
            continue
    return accumulator.result()


def _identity(item):
    """Stable item identity when the event carries one; unknown identity is never invented."""
    for name in ("id", "call_id"):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if type(value) is int:
            return value
    return None


def tool_event(event):
    """Return ``(lifecycle stage or None, item key or None)`` for a documented tool event.

    ``item.started``/``item.completed`` name a tool in their explicit ``item``; a bare
    top-level tool event (``function_call``, ``web_search`` ...) names one directly. A nested
    ``type`` value inside payload data is content the worker quotes, never tool evidence.
    ``None`` means the event is not about a tool.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind in TOOL_LIFECYCLE:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in TOOLS:
            return None
        return ("started" if kind == "item.started" else "completed"), _identity(item)
    if isinstance(kind, str) and kind in TOOLS:
        return None, _identity(event)
    return None


def terminal_failure(event):
    """Event type of a documented terminal failure event, else ``None``.

    A tool command that failed and was recovered from is an ``item.*`` event and stays
    non-terminal; only the CLI's own ``turn.failed``/``response.failed``/``error`` events end
    the run, and only when they are the event itself rather than quoted payload content.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    return kind if isinstance(kind, str) and kind in TERMINAL_EVENT_TYPES else None


def event_thread_id(event):
    """Thread identity of the documented top-level ``thread.started`` event, else ``None``."""
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return None
    value = event.get("thread_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def event_models(event):
    """Model identity from explicit event metadata only: top level and response metadata.

    Tool output, file content, reasoning and message payloads routinely quote model names;
    recursively scanning for a ``model`` key would turn that data into routing evidence.
    """
    if not isinstance(event, dict):
        return []
    models = []
    for node in (event, event.get("response")):
        if not isinstance(node, dict):
            continue
        value = node.get("model")
        if isinstance(value, str) and value.strip() and value.strip() not in models:
            models.append(value.strip())
    return models


def sum_usage(evidence):
    """Combine every attempt, reporting unknown and partially covered counts honestly."""
    attempts = list(evidence)
    rows = []
    sources = set()
    with_usage = 0
    complete = bool(attempts)
    durations = []
    starts, finishes = [], []
    observed = 0
    for attempt in attempts:
        usage = attempt.get("usage") or {}
        counts = {name: _number(usage.get(name, (attempt.get("token_counts") or {}).get(name)))
                  for name in FIELDS}
        rows.append(counts)
        known = any(value is not None for value in counts.values())
        with_usage += known
        complete = complete and bool(usage.get("complete"))
        sources.update(usage.get("sources") or ([usage["source"]] if usage.get("source") else []))
        observed += usage.get("observed_records", 0)
        duration = attempt.get("elapsed_seconds", usage.get("elapsed_seconds"))
        if type(duration) in (int, float) and duration >= 0:
            durations.append(duration)
        if isinstance(usage.get("started_at"), str):
            starts.append(usage["started_at"])
        if isinstance(usage.get("finished_at"), str):
            finishes.append(usage["finished_at"])
    result = _result(rows, sources)
    usage = result["usage"]
    usage.update(complete=complete, availability=("complete" if complete else
                 "partial" if with_usage else "unavailable"), observed_records=observed,
                 started_at=min(starts) if starts else None,
                 finished_at=max(finishes) if finishes else None,
                 elapsed_seconds=round(sum(durations), 3) if len(durations) == len(attempts) and attempts else None)
    usage["coverage"].update(attempts_total=len(attempts), attempts_with_usage=with_usage,
                            attempts_with_duration=len(durations))
    return result


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate(process):
    # start_new_session makes pid the stable pgid even if the leader has exited.
    pgid = process.pid
    outcome = {"terminated": False, "force_killed": False, "survivors": False}
    if _group_exists(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
            outcome["terminated"] = True
        until = time.monotonic() + .2
        while time.monotonic() < until:
            process.poll()
            if not _group_exists(pgid):
                break
            time.sleep(.01)
        if _group_exists(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
                outcome["force_killed"] = True
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=.5)
    outcome["survivors"] = _group_exists(pgid)
    return outcome


def run_streaming(argv, *, cwd, env, timeout, stdin_data=None, observer=None,
                  terminate_group=None, capture_limit_bytes=None, stall_seconds=None,
                  on_stall=None, popen_factory=None):
    """Drain both pipes and feed stdin concurrently, bounded even with orphaned pipes.

    The callback receives raw bytes and must sanitize before persistence. Returned
    byte buffers retain run_bounded compatibility; callers redact final reports.

    Retention is capped per stream: past ``CAPTURE_LIMIT_BYTES`` the process group is
    terminated, the extra bytes are dropped without reaching the buffers or the observer,
    and the pipes are drained only for the existing half-second cleanup window.
    ``output_limit_exceeded`` reports that truncation so no caller can read success out of
    output it never saw.
    """
    limit = CAPTURE_LIMIT_BYTES if capture_limit_bytes is None else max(0, int(capture_limit_bytes))
    started = time.monotonic()
    deadline = started + max(0, timeout)
    factory = popen_factory or subprocess.Popen
    process = factory(list(argv), cwd=str(cwd) if cwd else None, env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    termination = {"terminated": False, "force_killed": False, "survivors": False}
    timed_out = False
    stalled = False
    output_limit_exceeded = False
    drain_deadline = None
    pending = memoryview(stdin_data or b"")
    stall_timeout = (float(stall_seconds) if stall_seconds is not None and
                     float(stall_seconds) > 0 else None)
    stall_deadline = started + stall_timeout if stall_timeout is not None else None

    def stop():
        outcome = terminate_group(process) if terminate_group else _terminate(process)
        # An exited leader can make legacy getpgid-based callbacks miss descendants.
        if terminate_group and _group_exists(process.pid):
            extra = _terminate(process)
            outcome = {name: bool(outcome.get(name) or extra[name]) for name in termination}
            outcome["survivors"] = extra["survivors"]
        return outcome

    def close(pipe):
        with contextlib.suppress(KeyError):
            selector.unregister(pipe)
        pipe.close()

    try:
        process_started = getattr(observer, "process_started", None)
        if callable(process_started):
            process_started(process.pid)
        for name in buffers:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if process.stdin:
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                if stalled:
                    break
                if not timed_out:
                    timed_out = True
                    termination = stop()
                    deadline = time.monotonic() + .5
                else:
                    break
            if stall_deadline is not None and now >= stall_deadline and not timed_out:
                extend = False
                if callable(on_stall):
                    try:
                        extend = bool(on_stall())
                    except Exception:
                        extend = False
                if extend:
                    stall_deadline = now + stall_timeout
                else:
                    stalled = True
                    termination = stop()
                    deadline = time.monotonic() + .5
                    stall_deadline = None
            wait = min(.05, max(0, deadline - time.monotonic()))
            if drain_deadline is not None:
                if time.monotonic() >= drain_deadline:
                    break
                wait = min(wait, max(0, drain_deadline - time.monotonic()))
            for key, _ in selector.select(wait):
                pipe, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        sent = os.write(pipe.fileno(), pending[:65536])
                        pending = pending[sent:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        close(pipe)
                    continue
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    close(pipe)
                    continue
                if stall_timeout is not None:
                    stall_deadline = time.monotonic() + stall_timeout
                retained = limit - len(buffers[name])
                if retained > 0:
                    buffers[name].extend(chunk[:retained])
                    if observer:
                        observer(name, chunk[:retained])
                if len(chunk) > retained and not output_limit_exceeded:
                    # Everything past the cap is dropped unseen: kill the group, stop feeding
                    # stdin, and keep draining only until the cleanup window closes.
                    output_limit_exceeded = True
                    termination = stop()
                    drain_deadline = time.monotonic() + .5
                    pending = pending[:0]
                    if process.stdin is not None and process.stdin in selector.get_map():
                        close(process.stdin)
        # The output-limit path already stopped the group: stopping again would replace the
        # record of what the first kill actually did.
        if process.poll() is None and not output_limit_exceeded:
            termination = stop()
    except BaseException:
        stop()
        raise
    finally:
        for key in list(selector.get_map().values()):
            close(key.fileobj)
        selector.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=.5)
    return {"argv": list(argv), "returncode": process.returncode, "timed_out": timed_out,
            "stalled": stalled,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout": bytes(buffers["stdout"]), "stderr": bytes(buffers["stderr"]),
            "termination": termination, "output_limit_exceeded": output_limit_exceeded}


class _Redactor:
    """Retain only a suffix that may become a secret when the next chunk arrives."""
    def __init__(self, secrets):
        self.secrets = sorted({value for value in secrets if isinstance(value, str) and value},
                              key=len, reverse=True)
        self.pending = ""
        self.max_length = max((len(secret) for secret in self.secrets), default=0)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk, final=False):
        value = self.pending + self.decoder.decode(chunk, final=final)
        if not self.secrets:
            self.pending = ""
            return value
        safe_end = len(value) if final else max(0, len(value) - self.max_length + 1)
        if not final:
            # A shorter secret can start just before the held suffix. Move the cut back until
            # no match is split; this also keeps replacement markers wholly in one output.
            while True:
                earlier = safe_end
                for secret in self.secrets:
                    start = value.find(secret, max(0, safe_end - len(secret) + 1))
                    if 0 <= start < safe_end and start + len(secret) > safe_end:
                        earlier = min(earlier, start)
                if earlier == safe_end:
                    break
                safe_end = earlier
        output = value[:safe_end]
        self.pending = "" if final else value[safe_end:]
        for secret in self.secrets:
            output = output.replace(secret, "[REDACTED]")
        return output


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


class RunObserver:
    """Persist sanitized output immediately and replace an atomic progress snapshot."""
    def __init__(self, report_dir, secrets=(), attempt=1, job_id=None, job=None,
                 active_dir=None, worker_command=None):
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.started_at = _now()
        # ``progress.json`` is shared with the launcher: it owns job state, this observer owns
        # attempt state. Job facts are carried into every snapshot the observer writes, so a
        # quiet worker cannot make a live job look finished.
        self.job = dict(job or {})
        if not all(self.job.get(name) for name in ("started_at", "workstream", "job_kind")):
            for name in ("started_at", "workstream", "job_kind"):
                if not self.job.get(name):
                    self.job[name] = self._inherited().get(name)
        secrets = tuple(secrets)
        self.redactors = {name: _Redactor(secrets) for name in ("stdout", "stderr")}
        redactor = _Redactor(secrets)
        self.job_id = redactor.feed(str(job_id or "").encode(), final=True) or None
        self.active_dir = Path(active_dir) if active_dir is not None else None
        self.worker_command = str(worker_command or "")
        self.active_path = (self.active_dir / f"{self.job_id}.json"
                            if self.active_dir is not None and self.job_id else None)
        self.attempt = attempt
        self.launcher_pid = os.getpid()
        self.launcher_start_identity = process_supervisor.process_start_identity(self.launcher_pid)
        self.worker_pid = None
        self.files = {}
        for name, suffix in (("stdout", ".jsonl"), ("stderr", ".stderr.log")):
            path = self.report_dir / f"worker-attempt-{attempt}{suffix}"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self.files[name] = os.fdopen(fd, "w", encoding="utf-8")
        self.status = "worker_processing"
        self.finished_at = None
        self.finished_elapsed = None
        self.last_activity_at = None
        self.last_event_at = None
        self.activity_count = 0
        self.events = EventAccumulator()
        self.line = ""
        self.closed = False
        self._last_progress_write = 0.0
        self._last_progress_status = None
        self._write_progress()

    def process_started(self, pid):
        """Record the actual worker process group for interruption and orphan recovery."""
        self.worker_pid = pid
        if self.active_path is not None:
            self.active_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.active_path, {
                "schema": "local-worker-active-group/1",
                "job_id": self.job_id, "launcher_pid": self.launcher_pid,
                "launcher_start_identity": self.launcher_start_identity,
                "worker_pgid": pid, "worker_command": self.worker_command,
                "owner_pid": pid,
                "owner_start_identity": process_supervisor.process_start_identity(pid),
                "owner_sid": pid,
                "started_at": self.started_at,
            })
        self._write_progress(force=True)

    def _inherited(self):
        """Job metadata the launcher already wrote into this attempt's progress file."""
        try:
            value = json.loads((self.report_dir / "progress.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _job_metadata(self):
        """The job-level keys a snapshot must never drop.

        A finished worker is not a finished job: only the launcher's final progress record marks
        the job inactive, so ``status`` can still check launcher liveness on a quiet snapshot.
        """
        return dict(active=True, workstream=self.job.get("workstream"),
                    job_kind=self.job.get("job_kind"),
                    job_started_at=self.job.get("started_at") or self.started_at)

    def _event(self, event):
        self.events.add(event)
        detected = tool_event(event)
        if detected is None:
            return
        stage, _key = detected
        if stage is None:
            # A bare top-level tool event names a tool but has no lifecycle stage of its own.
            return
        name = "tool_started" if stage == "started" else "tool_completed"
        self.status = name

    def _persist(self, name, text):
        if not text:
            return
        self.files[name].write(text)
        self.files[name].flush()
        if name == "stdout":
            self.line += text
            while "\n" in self.line:
                line, self.line = self.line.split("\n", 1)
                self.last_event_at = _now()
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    event = None
                if isinstance(event, dict):
                    self.status = "worker_processing"
                    self._event(event)
                else:
                    self.events.add_raw_line(line)

    def __call__(self, stream, chunk):
        if self.closed:
            raise RuntimeError("observer is already finished")
        self.status = "worker_processing"
        self.last_activity_at = _now()
        self.activity_count += 1
        self._persist(stream, self.redactors[stream].feed(chunk))
        self._write_progress()

    def evidence(self):
        return self.events.result()

    def snapshot(self):
        evidence = self.evidence()
        usage = evidence["usage"]
        if self.status in {"failed", "timed_out"}:
            usage["complete"] = False
            usage["availability"] = ("partial" if any(usage[name] is not None for name in FIELDS)
                                     else "unavailable")
        usage.update(started_at=self.started_at, finished_at=self.finished_at,
                     elapsed_seconds=(self.finished_elapsed if self.finished_elapsed is not None else
                                      round(time.monotonic() - self.started, 3)))
        return dict(job_id=self.job_id, attempt=self.attempt, status=self.status,
                    phase=self.status, last_event_at=self.last_event_at,
                    thread_id=evidence["thread_id"], terminal_failures=list(evidence["terminal_failures"]),
                    launcher_pid=self.launcher_pid, worker_pid=self.worker_pid,
                    started_at=self.started_at, finished_at=self.finished_at,
                    last_activity_at=self.last_activity_at, activity_count=self.activity_count,
                    elapsed_seconds=usage["elapsed_seconds"], usage=usage,
                    tool_started_count=evidence["tool_started_count"],
                    tool_completed_count=evidence["tool_completed_count"], **self._job_metadata())

    def _write_progress(self, force=False):
        now = time.monotonic()
        if (not force and self.status == self._last_progress_status and
                now - self._last_progress_write < .5):
            return
        _atomic_json(self.report_dir / "progress.json", self.snapshot())
        self._last_progress_write = now
        self._last_progress_status = self.status

    def _close_files(self):
        for handle in getattr(self, "files", {}).values():
            with contextlib.suppress(Exception):
                if not handle.closed:
                    handle.close()

    def finish(self, status="completed", result=None):
        if self.closed:
            return
        try:
            for name, redactor in self.redactors.items():
                self._persist(name, redactor.feed(b"", final=True))
            if self.line:
                self.last_event_at = _now()
                self.events.add_raw_line(self.line)
        finally:
            self._close_files()
            if self.active_path is not None:
                with contextlib.suppress(OSError):
                    self.active_path.unlink()
        if result and result.get("timed_out"):
            status = "timed_out"
        elif result and result.get("returncode") not in (None, 0) and status == "completed":
            status = "failed"
        if self.events.result()["terminal_failures"] and status not in ("failed", "timed_out"):
            # A documented terminal event outranks a zero exit code: the run never ended well.
            status = "failed"
        self.status = status
        self.finished_at = _now()
        self.finished_elapsed = round(time.monotonic() - self.started, 3)
        self.closed = True
        self._write_progress(force=True)

    def __del__(self):
        self._close_files()
