#!/usr/bin/env python3
"""Bounded shared-state concurrency for local implementation workers.

Standard library only, no git, no subprocess, no model or sandbox policy. Every
coordination primitive here is an OS file lock inside one shared state directory, so the
pool degrades honestly: a lock is either held by a live process or it is not.

Layout under one shared ``<state_dir>``:

* ``worker.lock``   - the legacy single-worker lock. Exclusive at capacity 1 (exact old
  behaviour, PID text included); shared above 1, so a default/legacy job and a parallel
  job are mutually exclusive in both directions. Held for the whole job.
* ``coordinator.lock`` - taken only briefly to read live lease records, check owned-path
  overlap and claim a slot, and released only after the new owner's record is published, so a
  live slot never exists without the ownership that belongs to it. Never held while blocking,
  so it cannot deadlock.
* ``slots/slot-N.lock`` - at most ``MAX_WORKERS_LIMIT`` exclusive slot leases.
* ``leases/slot-N.json`` - claim metadata, written by the lease owner inside its claim pass
  and on later updates, always bounded by the job deadline the owner was given.
* ``resources/apple-simulator-<uuid>.lock`` - exclusive per-device resource lease.

The flock state in the kernel is authoritative: ``leases/*.json`` is metadata only, and a
record whose slot lock is free is stale, never reported as active. A killed holder
releases every lock because the OS closes its file descriptors.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MAX_WORKERS = 1
MAX_WORKERS_LIMIT = 4
POLL_SECONDS = 0.02
LOCK_TIMEOUT_SECONDS = 30.0

WORKER_LOCK = "worker.lock"
COORDINATOR_LOCK = "coordinator.lock"
SLOT_DIR = "slots"
LEASE_DIR = "leases"
RESOURCE_DIR = "resources"

WOULD_BLOCK = (errno.EAGAIN, errno.EACCES)
_RESOURCE_KEY = re.compile(r"^[0-9a-z][0-9a-z._:-]{0,127}$")
# Cleanup never claims a fresh budget: it gets whatever the job deadline leaves, clipped to
# this small grace, and otherwise leaves inactive metadata behind instead of blocking.
FORGET_GRACE_SECONDS = 0.25


class PoolError(Exception):
    """Base class for pool failures."""


class PoolConfigError(PoolError):
    """The requested capacity or owned path is invalid; nothing was claimed."""


class PoolUnavailableError(PoolError):
    """Capacity is exhausted for now; nothing was claimed."""


class PoolConflictError(PoolError):
    """An active lease owns overlapping paths; retrying cannot succeed."""


def validate_max_workers(value: Any) -> int:
    """Hard-validate the worker ceiling: integer 1..4, bools and everything else refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PoolConfigError(
            f"max_workers must be an integer between 1 and {MAX_WORKERS_LIMIT}, "
            f"got {value!r}"
        )
    if not 1 <= value <= MAX_WORKERS_LIMIT:
        raise PoolConfigError(
            f"max_workers must be between 1 and {MAX_WORKERS_LIMIT}, got {value!r}"
        )
    return value


def canonical_repo_path(path: Any) -> str:
    """Canonical identity of a repository or worktree path."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _resource_key(udid: Any) -> str:
    key = str(udid).strip().lower()
    if not _RESOURCE_KEY.match(key):
        raise PoolConfigError(f"invalid simulator resource identifier: {udid!r}")
    return key


def _ownership_root(spec: Any) -> str:
    """Normalize an owned path into the deepest glob-free directory it can claim.

    ``src/app.py`` -> ``src/app.py``; ``src/*`` -> ``src``; ``*.md`` -> ``""`` (claims
    the repository root, therefore overlapping with everything).
    """
    text = str(spec).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if not text or text == ".":
        return ""
    head: List[str] = []
    for part in text.split("/"):
        if any(character in part for character in "*?["):
            break
        head.append(part)
    return "/".join(head)


def _fold(path: Any) -> str:
    """Case-insensitive comparison key: a macOS volume maps two casings to one file."""
    return str(path).casefold()


def owned_paths_overlap(left: Sequence[str], right: Sequence[str]) -> Optional[Tuple[str, str]]:
    """Return the offending pair of owned specs when two contracts can touch one file.

    Owned roots are compared casefolded on purpose. On a case-insensitive volume
    ``Sources/Settings.swift`` and ``Sources/settings.swift`` are the same file, and a
    directory or glob-root case alias (``Sources/*`` vs ``sources/*``) is the same subtree.
    A false positive on a case-sensitive volume only costs a sequential retry; admitting a
    case alias would let two live jobs edit one file.
    """
    for left_spec in left:
        left_root = _fold(_ownership_root(left_spec))
        for right_spec in right:
            right_root = _fold(_ownership_root(right_spec))
            if not left_root or not right_root:
                return (str(left_spec), str(right_spec))
            if left_root == right_root or left_root.startswith(right_root + "/") \
                    or right_root.startswith(left_root + "/"):
                return (str(left_spec), str(right_spec))
    return None


def _open_lock(state_dir: Path, relative: str) -> Any:
    path = state_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+", encoding="utf-8")


def _try(handle: Any, exclusive: bool = True) -> bool:
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in WOULD_BLOCK:
            return False
        raise PoolError(f"cannot acquire pool lock: {error}") from error


def _acquire(handle: Any, deadline: float, label: str, exclusive: bool = True) -> float:
    started = time.monotonic()
    while not _try(handle, exclusive):
        if time.monotonic() >= deadline:
            raise PoolUnavailableError(f"timed out after {time.monotonic() - started:.2f}s "
                                       f"waiting for pool {label}")
        time.sleep(POLL_SECONDS)
    return round(time.monotonic() - started, 3)


def _release(handle: Optional[Any]) -> None:
    if handle is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


def _lease_path(state_dir: Path, slot: int) -> Path:
    return state_dir / LEASE_DIR / f"slot-{slot}.json"


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Atomic parent-only metadata write: temp file in the same directory, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".tmp-", suffix=".json", delete=False)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {"record_unreadable": True}
    except (OSError, ValueError):
        return {"record_unreadable": True}


@contextlib.contextmanager
def _coordinator(state_dir: Path, deadline: float):
    handle = _open_lock(state_dir, COORDINATOR_LOCK)
    try:
        _acquire(handle, deadline, COORDINATOR_LOCK)
        yield
    finally:
        _release(handle)


def slot_is_live(state_dir: Path, slot: int) -> bool:
    """True when a live process holds this slot lock. A leftover file is not active."""
    path = state_dir / SLOT_DIR / f"slot-{slot}.lock"
    if not path.exists():
        return False
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return True
    try:
        return not _try(handle)
    finally:
        _release(handle)


def active_leases(state_dir: Path) -> List[Dict[str, Any]]:
    """Live lease records, decided by the kernel lock state, never by file presence."""
    state_dir = Path(state_dir)
    found: List[Dict[str, Any]] = []
    for slot in range(1, MAX_WORKERS_LIMIT + 1):
        if not slot_is_live(state_dir, slot):
            continue
        record = _read_json(_lease_path(state_dir, slot))
        record.setdefault("task_id", "unknown")
        record["slot"] = slot
        found.append(record)
    return found


def describe_active(state_dir: Path) -> str:
    active = active_leases(state_dir)
    if not active:
        return "no active lease"
    parts = [f"slot {item['slot']} job {item.get('task_id')}" for item in active]
    return ", ".join(parts)


@dataclass
class Lease:
    """One claimed worker slot plus the metadata that describes its contract."""

    state_dir: Path
    slot: Optional[int]
    capacity: int
    task_id: str
    repo_identity: str
    allowed_paths: List[str]
    job_id: Optional[str] = None
    base_ref: Optional[str] = None
    worktree: Optional[str] = None
    report_dir: Optional[str] = None
    resource: Optional[str] = None
    waits: Dict[str, float] = field(default_factory=dict)
    token: str = ""
    metadata_deadline: Optional[float] = None

    @property
    def mode(self) -> str:
        return "exclusive" if self.capacity == 1 else "parallel"

    @property
    def wait_seconds(self) -> float:
        return round(sum(self.waits.values()), 3)

    def metadata_budget(self) -> float:
        """Absolute monotonic bound for metadata writes; never a freshly claimed 30 s.

        The pool passes the job deadline it was handed, falling back to the admission deadline
        it already knows. A ``Lease`` built by hand with no deadline keeps the pool default.
        """
        if self.metadata_deadline is not None:
            return float(self.metadata_deadline)
        return time.monotonic() + LOCK_TIMEOUT_SECONDS

    def record(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "repo_identity": self.repo_identity,
            "allowed_paths": list(self.allowed_paths),
            "base_ref": self.base_ref,
            "worktree": self.worktree,
            "report_dir": self.report_dir,
            "resource": self.resource,
            "slot": self.slot,
            "capacity": self.capacity,
            "pid": os.getpid(),
            "claimed_at": time.time(),
        }

    def _publish_locked(self) -> None:
        """Write this lease's own record while the caller already holds the coordinator.

        Used for the initial publication inside the claim critical section. Taking the
        coordinator again from inside itself would deadlock on the same flock, so this helper
        must never be called without the lock held by the caller.
        """
        if self.slot is None:
            return
        _write_json(_lease_path(self.state_dir, self.slot), self.record())

    def _rewrite(self) -> None:
        if self.slot is None:
            return
        path = _lease_path(self.state_dir, self.slot)
        with _coordinator(self.state_dir, self.metadata_budget()):
            _write_json(path, self.record())

    def update(self, **values: Any) -> None:
        """Owner-only metadata update; the worktree and report dir are known only later."""
        for name, value in values.items():
            if name not in {"worktree", "report_dir", "base_ref", "job_id"}:
                raise PoolConfigError(f"lease field is not updatable: {name}")
            setattr(self, name, value)
        self._rewrite()

    def forget(self) -> bool:
        """Delete this lease's own record; never touches another job's file.

        Cleanup is bounded by whatever the job deadline still allows, clipped to
        ``FORGET_GRACE_SECONDS``, and never claims a fresh window. If the coordinator is busy
        or the budget is spent the inactive record is left in place: kernel lock state decides
        activity, so leftover metadata is harmless, while a 30 s block would spend budget the
        job never had and delay releasing the slot, device and legacy descriptors.
        """
        if self.slot is None:
            return False
        path = _lease_path(self.state_dir, self.slot)
        grace = max(0.0, min(FORGET_GRACE_SECONDS, self.metadata_budget() - time.monotonic()))
        handle = _open_lock(self.state_dir, COORDINATOR_LOCK)
        try:
            try:
                _acquire(handle, time.monotonic() + grace, COORDINATOR_LOCK)
            except PoolUnavailableError:
                return False
            if _read_json(path).get("token") == self.token:
                with contextlib.suppress(OSError):
                    path.unlink()
            return True
        finally:
            _release(handle)

    def as_report(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "capacity": self.capacity,
            "slot": self.slot,
            "legacy_lock": "exclusive" if self.capacity == 1 else "shared",
            "resource": self.resource,
            "wait_seconds": self.wait_seconds,
            "waits": dict(self.waits),
            "pool": str(self.state_dir),
            "task_id": self.task_id,
            "repo_identity": self.repo_identity,
        }


def _owner_unverified(record: Dict[str, Any]) -> bool:
    """True when a live slot cannot prove which job and repository it belongs to."""
    if record.get("record_unreadable"):
        return True
    for name in ("repo_identity", "task_id"):
        value = record.get(name)
        if not isinstance(value, str) or not value.strip() or value.strip() == "unknown":
            return True
    return not isinstance(record.get("allowed_paths"), list)


def _admission_refusal(records: Sequence[Dict[str, Any]], repo_identity: str,
                       allowed_paths: Sequence[str]) -> Optional[PoolError]:
    """Decide admission from live records, read inside the coordinator critical section.

    Ownership is checked before overlap and it fails closed: a live slot whose record is
    missing, unreadable or names an unknown owner proves nothing, so disjointness cannot be
    established and admission is refused for now instead of skipping the slot. Metadata left
    on a free slot is not activity and is never in ``records`` in the first place.
    """
    for record in records:
        if _owner_unverified(record):
            return PoolUnavailableError(
                f"lease slot {record.get('slot')} is held by a job whose ownership metadata "
                f"is missing or unreadable, so path disjointness cannot be verified; nothing "
                f"was claimed. Retry shortly, once that owner has published its lease record."
            )
    for record in records:
        if _fold(record.get("repo_identity")) != _fold(repo_identity):
            continue
        pair = owned_paths_overlap(allowed_paths, record.get("allowed_paths") or [])
        if pair is None:
            continue
        return PoolConflictError(
            f"lease slot {record.get('slot')} is busy: job {record.get('task_id')} "
            f"(job_id {record.get('job_id')}) owns {pair[1]!r} in the same repository "
            f"{record.get('repo_identity')}, which overlaps this contract's {pair[0]!r}. "
            "Separate worktrees alone do not make overlapping edits mergeable; run these "
            "jobs sequentially or give the contracts disjoint owned paths."
        )
    return None


def _effective_capacity(records: Sequence[Dict[str, Any]], capacity: int) -> int:
    """Honor the lowest active parallel ceiling so a mixed 2/4 pool never exceeds 2.

    The hard ceiling stays ``MAX_WORKERS_LIMIT`` and a default/legacy job still means one
    writer; an unreadable or corrupt declared ceiling is treated as the strictest value.
    """
    limit = capacity
    for record in records:
        declared = record.get("capacity")
        ceiling = 1
        if not isinstance(declared, bool) and isinstance(declared, int) \
                and 1 <= declared <= MAX_WORKERS_LIMIT:
            ceiling = declared
        limit = min(limit, ceiling)
    return max(1, min(limit, MAX_WORKERS_LIMIT))


@contextlib.contextmanager
def _simulator_resources(state_dir: Path, udids: Sequence[str], *,
                        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
                        job_deadline: Optional[float] = None):
    """Hold an ordered set of simulator locks from the pool's shared resource namespace.

    Capture jobs can visit several devices. Callers must acquire this set after the legacy
    worker lock and before taking a worker slot. The complete set is normalized and acquired in
    sorted order, so captures cannot deadlock by holding opposite subsets. The deadline remains
    the caller's original job deadline.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    keys = sorted({_resource_key(udid) for udid in udids})
    deadline = time.monotonic() + max(float(lock_timeout), 0.0)
    if job_deadline is not None:
        deadline = min(deadline, float(job_deadline))
    handles = []
    try:
        for key in keys:
            handle = _open_lock(state_dir, f"{RESOURCE_DIR}/apple-simulator-{key}.lock")
            try:
                _acquire(handle, deadline, f"apple-simulator:{key}")
            except Exception:
                _release(handle)
                raise
            handles.append(handle)
        yield set(keys)
    finally:
        for handle in reversed(handles):
            _release(handle)


@contextlib.contextmanager
def lease(state_dir: Path, *, capacity: int = DEFAULT_MAX_WORKERS, task_id: str,
          repo_identity: str, allowed_paths: Sequence[str], job_id: Optional[str] = None,
          base_ref: Optional[str] = None, lock_timeout: float = LOCK_TIMEOUT_SECONDS,
          simulator_udid: Optional[str] = None,
          simulator_udids: Optional[Sequence[str]] = None,
          job_deadline: Optional[float] = None):
    """Claim one of at most ``capacity`` slots in one shared state directory.

    Locks are taken in a fixed order (legacy worker lock, resource lease, short
    coordinator pass, slot lease) so concurrent claimers cannot deadlock. Blocking happens
    only on the legacy and resource locks and on the slot retry loop, all inside
    ``lock_timeout`` and never past ``job_deadline``; the coordinator is held for one claim
    pass which includes publishing the new owner's record, so a competitor that wins the
    coordinator next always sees the ownership that belongs to a slot it found live - never a
    live slot with missing or stale metadata. An active same-repository owned-path overlap
    raises ``PoolConflictError`` immediately, because retrying cannot make overlapping edits
    mergeable, and a live slot whose ownership cannot be verified refuses admission rather
    than being skipped. Every lock taken here is released in ``finally``, and the kernel lock
    state - not a leftover record file - decides what is active.

    ``job_deadline`` is the absolute ``time.monotonic()`` end of the whole job, owned by the
    caller. Admission waits no longer than that, and every later metadata update and the
    cleanup are bounded by it too; nothing here resets or extends it.
    """
    capacity = validate_max_workers(capacity)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = canonical_repo_path(repo_identity)
    owned = [str(path) for path in allowed_paths]
    resource_keys = {_resource_key(udid) for udid in (simulator_udids or [])}
    if simulator_udid:
        resource_keys.add(_resource_key(simulator_udid))
    resource_keys = sorted(resource_keys)
    resource = (f"apple-simulator:{_resource_key(simulator_udid)}"
                if simulator_udid else None)
    deadline = time.monotonic() + max(float(lock_timeout), 0.0)
    if job_deadline is not None:
        deadline = min(deadline, float(job_deadline))
    metadata_deadline = float(job_deadline) if job_deadline is not None else deadline
    waits: Dict[str, float] = {}
    legacy = slot_handle = coordinator = None
    device_handles = []
    holder: Optional[Lease] = None
    try:
        legacy = _open_lock(state_dir, WORKER_LOCK)
        waits["legacy"] = _acquire(legacy, deadline, WORKER_LOCK, exclusive=(capacity == 1))
        resource_started = time.monotonic()
        for key in resource_keys:
            device = _open_lock(state_dir,
                                f"{RESOURCE_DIR}/apple-simulator-{key}.lock")
            try:
                _acquire(device, deadline, f"apple-simulator:{key}")
            except Exception:
                _release(device)
                raise
            device_handles.append(device)
        if resource_keys:
            waits["resource"] = round(time.monotonic() - resource_started, 3)
        claim_started = time.monotonic()
        while True:
            coordinator = _open_lock(state_dir, COORDINATOR_LOCK)
            try:
                _acquire(coordinator, deadline, COORDINATOR_LOCK)
                records = active_leases(state_dir)
                refusal = _admission_refusal(records, identity, owned)
                if refusal is not None:
                    raise refusal
                for slot in range(1, _effective_capacity(records, capacity) + 1):
                    candidate = _open_lock(state_dir, f"{SLOT_DIR}/slot-{slot}.lock")
                    if _try(candidate):
                        slot_handle = candidate
                        waits["slot"] = round(time.monotonic() - claim_started, 3)
                        break
                    candidate.close()
                if slot_handle is not None and holder is None:
                    # Claim, ownership construction and metadata publication are one atomic
                    # coordinator critical section: the coordinator is not released between
                    # the flock and the record, and the record write must not take the same
                    # flock again from inside itself.
                    holder = Lease(state_dir=state_dir, slot=slot, capacity=capacity,
                                   task_id=str(task_id), repo_identity=identity,
                                   allowed_paths=owned, job_id=job_id, base_ref=base_ref,
                                   resource=resource, waits=dict(waits),
                                   token=uuid.uuid4().hex,
                                   metadata_deadline=metadata_deadline)
                    holder._publish_locked()
            finally:
                _release(coordinator)
                coordinator = None
            if holder is not None:
                break
            if time.monotonic() >= deadline:
                raise PoolUnavailableError(
                    f"all {capacity} worker slot(s) of pool {state_dir} are busy; waited "
                    f"{time.monotonic() - claim_started:.2f}s, {describe_active(state_dir)}"
                )
            time.sleep(POLL_SECONDS)
        yield holder
    finally:
        if holder is not None:
            with contextlib.suppress(Exception):
                holder.forget()
        for handle in [slot_handle, *reversed(device_handles), legacy]:
            _release(handle)
