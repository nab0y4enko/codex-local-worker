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
TOOLS = {"command_execution", "function_call", "exec_command", "mcp_tool_call", "web_search"}


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


def run_streaming(argv, *, cwd, env, timeout, stdin_data=None, observer=None, terminate_group=None):
    """Drain both pipes and feed stdin concurrently, bounded even with orphaned pipes.

    The callback receives raw bytes and must sanitize before persistence. Returned
    byte buffers retain run_bounded compatibility; callers redact final reports.
    """
    started = time.monotonic()
    deadline = started + max(0, timeout)
    process = subprocess.Popen(list(argv), cwd=str(cwd) if cwd else None, env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    termination = {"terminated": False, "force_killed": False, "survivors": False}
    timed_out = False
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
            for key, _ in selector.select(min(.05, max(0, deadline - time.monotonic()))):
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
                buffers[name].extend(chunk)
                if observer:
                    observer(name, chunk)
        if process.poll() is None:
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
            "termination": termination}


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
    def __init__(self, report_dir, secrets=(), attempt=1, job_id=None):
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.started_at = _now()
        secrets = tuple(secrets)
        self.redactors = {name: _Redactor(secrets) for name in ("stdout", "stderr")}
        redactor = _Redactor(secrets)
        self.job_id = redactor.feed(str(job_id or "").encode(), final=True) or None
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
        self.counts = {"tool_started_count": 0, "tool_completed_count": 0}
        self.line = ""
        self.usage = _UsageAccumulator()
        self.closed = False
        self._write_progress()

    def process_started(self, pid):
        """Record the actual Popen PID; liveness interpretation belongs to the caller."""
        self.worker_pid = pid
        self._write_progress()

    def _event(self, event):
        self.usage.add(event)
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        item = event.get("item")
        if kind in {"item.started", "item.completed"} and isinstance(item, dict) and item.get("type") in TOOLS:
            self.status = "tool_started" if kind == "item.started" else "tool_completed"
            self.counts[self.status + "_count"] += 1

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
                    launcher_pid=self.launcher_pid, worker_pid=self.worker_pid,
                    started_at=self.started_at, finished_at=self.finished_at,
                    last_activity_at=self.last_activity_at, activity_count=self.activity_count,
                    elapsed_seconds=usage["elapsed_seconds"], usage=usage, **self.counts)

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
        if result and result.get("timed_out"):
            status = "timed_out"
        elif result and result.get("returncode") not in (None, 0) and status == "completed":
            status = "failed"
        self.status = status
        self.finished_at = _now()
        self.finished_elapsed = round(time.monotonic() - self.started, 3)
        self.closed = True
        self._write_progress()
