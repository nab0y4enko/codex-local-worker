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
from pathlib import Path
import selectors
import signal
import subprocess
import time


FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")
# A worker or a test command can flood its pipes. Memory stays bounded by retaining at
# most this many bytes per stream; the caller treats the truncation as a failure.
CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
TOOLS = {"command_execution", "function_call", "exec_command", "mcp_tool_call", "web_search"}
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
                  terminate_group=None, capture_limit_bytes=None):
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
    process = subprocess.Popen(list(argv), cwd=str(cwd) if cwd else None, env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    termination = {"terminated": False, "force_killed": False, "survivors": False}
    timed_out = False
    output_limit_exceeded = False
    drain_deadline = None
    pending = memoryview(stdin_data or b"")

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
                if not timed_out:
                    timed_out = True
                    termination = stop()
                    deadline = time.monotonic() + .5
                else:
                    break
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
        out, index = [], 0
        while index < len(value):
            if not final and len(value) - index < self.max_length:
                remaining = value[index:]
                if any(secret.startswith(remaining) and len(secret) > len(remaining)
                       for secret in self.secrets):
                    break
            secret = next((secret for secret in self.secrets if value.startswith(secret, index)), None)
            if secret:
                out.append("[REDACTED]")
                index += len(secret)
            else:
                out.append(value[index])
                index += 1
        self.pending = value[index:]
        return "".join(out)


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
        self.activity_count = 0
        self.thread_id = None
        self.terminal_failures = []
        # Separate identity sets per stage: a duplicate ``item.started`` is one tool, and a
        # payload that merely contains a ``type`` value is never counted at all.
        self.tool_ids = {"tool_started": set(), "tool_completed": set()}
        self.tool_anonymous = {"tool_started": 0, "tool_completed": 0}
        self.line = ""
        self.usage = _UsageAccumulator()
        self.closed = False
        self._write_progress()

    def process_started(self, pid):
        """Record the actual worker process group for interruption and orphan recovery."""
        self.worker_pid = pid
        if self.active_path is not None:
            self.active_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.active_path, {
                "job_id": self.job_id, "launcher_pid": self.launcher_pid,
                "worker_pgid": pid, "worker_command": self.worker_command,
                "started_at": self.started_at,
            })
        self._write_progress()

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
        self.usage.add(event)
        if not isinstance(event, dict):
            return
        thread = event_thread_id(event)
        if thread and self.thread_id is None:
            self.thread_id = thread
        failure = terminal_failure(event)
        if failure and failure not in self.terminal_failures:
            self.terminal_failures.append(failure)
        detected = tool_event(event)
        if detected is None:
            return
        stage, key = detected
        if stage is None:
            # A bare top-level tool event names a tool but has no lifecycle stage of its own.
            return
        name = "tool_started" if stage == "started" else "tool_completed"
        if key is None:
            # Without an id there is nothing to deduplicate against, so it stays an observation.
            self.tool_anonymous[name] += 1
        else:
            self.tool_ids[name].add(key)
        self.status = name

    def _tool_counts(self):
        return dict((name + "_count", len(self.tool_ids[name]) + self.tool_anonymous[name])
                    for name in self.tool_ids)

    def record_event_evidence(self, evidence):
        """Reconcile the snapshot with what the persisted JSONL actually contains.

        The live callback only sees chunks the reader delivered while the worker ran, so thread
        identity and terminal events are adopted from the parsed stream before the final snapshot
        is written. Tool counts stay what the live stream showed: they are a progress signal.
        """
        if not isinstance(evidence, dict):
            return
        thread = evidence.get("thread_id")
        if thread and self.thread_id is None:
            self.thread_id = thread
        for failure in evidence.get("terminal_failures") or []:
            if failure not in self.terminal_failures:
                self.terminal_failures.append(failure)
        if self.terminal_failures and self.status not in ("failed", "timed_out"):
            self.status = "failed"
        self._write_progress()

    def _persist(self, name, text):
        if not text:
            return
        self.files[name].write(text)
        self.files[name].flush()
        if name == "stdout":
            self.line += text
            while "\n" in self.line:
                line, self.line = self.line.split("\n", 1)
                try:
                    self._event(json.loads(line))
                except (ValueError, TypeError):
                    continue

    def __call__(self, stream, chunk):
        if self.closed:
            raise RuntimeError("observer is already finished")
        self.status = "worker_processing"
        self.last_activity_at = _now()
        self.activity_count += 1
        self._persist(stream, self.redactors[stream].feed(chunk))
        self._write_progress()

    def snapshot(self):
        usage = self.usage.result()["usage"]
        if self.status in {"failed", "timed_out"}:
            usage["complete"] = False
            usage["availability"] = ("partial" if any(usage[name] is not None for name in FIELDS)
                                     else "unavailable")
        usage.update(started_at=self.started_at, finished_at=self.finished_at,
                     elapsed_seconds=(self.finished_elapsed if self.finished_elapsed is not None else
                                      round(time.monotonic() - self.started, 3)))
        return dict(job_id=self.job_id, attempt=self.attempt, status=self.status,
                    thread_id=self.thread_id, terminal_failures=list(self.terminal_failures),
                    launcher_pid=self.launcher_pid, worker_pid=self.worker_pid,
                    started_at=self.started_at, finished_at=self.finished_at,
                    last_activity_at=self.last_activity_at, activity_count=self.activity_count,
                    elapsed_seconds=usage["elapsed_seconds"], usage=usage,
                    **self._tool_counts(), **self._job_metadata())

    def _write_progress(self):
        _atomic_json(self.report_dir / "progress.json", self.snapshot())

    def finish(self, status="completed", result=None):
        if self.closed:
            return
        for name, redactor in self.redactors.items():
            self._persist(name, redactor.feed(b"", final=True))
        if self.line:
            with contextlib.suppress(ValueError, TypeError):
                self._event(json.loads(self.line))
        for handle in self.files.values():
            handle.close()
        if self.active_path is not None:
            with contextlib.suppress(OSError):
                self.active_path.unlink()
        if result and result.get("timed_out"):
            status = "timed_out"
        elif result and result.get("returncode") not in (None, 0) and status == "completed":
            status = "failed"
        if self.terminal_failures and status not in ("failed", "timed_out"):
            # A documented terminal event outranks a zero exit code: the run never ended well.
            status = "failed"
        self.status = status
        self.finished_at = _now()
        self.finished_elapsed = round(time.monotonic() - self.started, 3)
        self.closed = True
        self._write_progress()
