"""Durable per-repository, workstream, and tier budgets."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone

EMPTY_LIMIT = 2
REPAIR_ATTEMPTS = 2
REPAIR_SECONDS = 1200.0
CLOUD_ATTEMPTS = 2
CLOUD_SECONDS = 1200.0


class PolicyStop(RuntimeError):
    def __init__(self, message: str, next_action: str = "needs_user"):
        super().__init__(message)
        if next_action not in ("escalate_cloud", "needs_user"):
            raise ValueError("policy stop next_action must be escalate_cloud or needs_user")
        self.next_action = next_action


class Workstream:
    def __init__(self, state_dir, repo_identity, name="default", kind="implementation",
                 tier: str = "local"):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("workstream must be a stable 1-64 character name, without paths")
        if kind not in ("implementation", "repair"):
            raise ValueError("job kind must be implementation or repair")
        if tier not in ("local", "cloud"):
            raise ValueError("tier must be local or cloud")
        self.name, self.kind, self.tier = name, kind, tier
        self.repo_identity = str(repo_identity)
        key = hashlib.sha256((self.repo_identity + "\0" + name + "\0" + tier).encode()).hexdigest()
        self.path = Path(state_dir) / "workstreams" / (key + ".json")
        self.lock_path = self.path.with_suffix(".lock")
        legacy_key = hashlib.sha256((self.repo_identity + "\0" + name).encode()).hexdigest()
        self.legacy_path = Path(state_dir) / "workstreams" / (legacy_key + ".json")

    def _legacy_local_state(self):
        """Read and validate the pre-tier local record before seeding the tiered key."""
        if self.tier != "local" or not self.legacy_path.exists():
            return None
        legacy_lock_path = self.legacy_path.with_suffix(".lock")
        with legacy_lock_path.open("a") as legacy_lock:
            fcntl.flock(legacy_lock, fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(self.legacy_path.read_text())
                except (OSError, ValueError) as error:
                    raise PolicyStop("legacy workstream state unreadable; preserve it for review") from error
                if (not isinstance(state, dict) or type(state.get("version")) is not int
                        or state.get("version") != 1 or state.get("workstream") != self.name
                        or not isinstance(state.get("active"), dict)):
                    raise PolicyStop("legacy workstream state invalid; preserve it for review")
                for key in ("empty_streak", "repair_attempts", "repair_seconds"):
                    value = state.get(key)
                    if (type(value) not in (int, float) or value < 0
                            or not math.isfinite(value)):
                        raise PolicyStop("legacy workstream counters invalid; preserve them for review")
                if type(state["empty_streak"]) is not int or type(state["repair_attempts"]) is not int:
                    raise PolicyStop("legacy workstream counters invalid; preserve them for review")
                for reservation in state["active"].values():
                    if (not isinstance(reservation, dict)
                            or reservation.get("kind") not in ("implementation", "repair")):
                        raise PolicyStop("legacy workstream reservation invalid; preserve it for review")
                    for key in ("reserved_seconds", "reserved_attempts", "recorded", "started"):
                        value = reservation.get(key)
                        if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
                            raise PolicyStop("legacy workstream reservation invalid; preserve it for review")
                        if key != "reserved_seconds" and type(value) is not int:
                            raise PolicyStop("legacy workstream reservation counters invalid")
                    if reservation["recorded"] > reservation["started"]:
                        raise PolicyStop("legacy workstream completion exceeds admitted attempts")
                if state["active"]:
                    raise PolicyStop("legacy workstream has active reservations; preserve and reconcile it before retrying")
                migrated = dict(state, tier="local", cloud_attempts=0, cloud_seconds=0.0,
                                legacy_migrated_from=str(self.legacy_path))
                return migrated
            finally:
                fcntl.flock(legacy_lock, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            end = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise PolicyStop("workstream state is busy; stop instead of starting another repair")
                    time.sleep(.01)
            try:
                if self.path.exists():
                    try:
                        state = json.loads(self.path.read_text())
                    except (OSError, ValueError) as error:
                        raise PolicyStop("workstream state unreadable; preserve it for review") from error
                    numeric = ("empty_streak", "repair_attempts", "repair_seconds",
                               "cloud_attempts", "cloud_seconds")
                    if (not isinstance(state, dict) or state.get("version") != 1
                            or not isinstance(state.get("active"), dict)
                            or any(not isinstance(state.get(key), (int, float))
                                   or isinstance(state.get(key), bool) or state[key] < 0
                                   or not math.isfinite(state[key]) for key in numeric)):
                        raise PolicyStop("workstream state invalid; preserve it for review")
                else:
                    legacy = self._legacy_local_state()
                    if legacy is not None:
                        state = legacy
                    else:
                        state = dict(version=1, workstream=self.name, tier=self.tier,
                                     empty_streak=0, repair_attempts=0, repair_seconds=0.0,
                                     cloud_attempts=0, cloud_seconds=0.0, active={})
                if (state.get("workstream") != self.name or state.get("tier") != self.tier
                        or type(state.get("version")) is not int):
                    raise PolicyStop("workstream identity invalid; preserve it for review")
                for key in ("empty_streak", "repair_attempts", "cloud_attempts"):
                    if type(state[key]) is not int:
                        raise PolicyStop("workstream counters invalid; preserve them for review")
                for reservation in state["active"].values():
                    if (not isinstance(reservation, dict)
                            or reservation.get("kind") not in ("implementation", "repair")):
                        raise PolicyStop("workstream reservation invalid; preserve it for review")
                    for key in ("reserved_seconds", "reserved_attempts", "recorded", "started"):
                        value = reservation.get(key)
                        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                            raise PolicyStop("workstream reservation invalid; preserve it for review")
                        if key != "reserved_seconds" and type(value) is not int:
                            raise PolicyStop("workstream reservation counters invalid")
                    if reservation["recorded"] > reservation["started"]:
                        raise PolicyStop("workstream completion exceeds admitted attempts")
                yield state
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".policy-")
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(state, stream, indent=2)
                        stream.write("\n")
                    os.replace(tmp, self.path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def check(self, state):
        if self.tier == "local" and state["empty_streak"] >= EMPTY_LIMIT:
            raise PolicyStop(
                "two consecutive capability failures; preserve evidence and escalate this workstream",
                "escalate_cloud")
        if self.tier == "local" and self.kind == "repair":
            if state["repair_attempts"] >= REPAIR_ATTEMPTS or state["repair_seconds"] >= REPAIR_SECONDS:
                raise PolicyStop("repair workstream exhausted its two-attempt / 1200-second allowance; no automatic renewal",
                                 "escalate_cloud")
        if self.tier == "cloud" and (state["cloud_attempts"] >= CLOUD_ATTEMPTS
                or state["cloud_seconds"] >= CLOUD_SECONDS):
            raise PolicyStop("cloud workstream exhausted its separate two-attempt / 1200-second allowance",
                             "needs_user")

    def claim(self, job_id, requested_seconds, max_attempts) -> dict:
        if not math.isfinite(requested_seconds) or requested_seconds <= 0 or max_attempts not in (1, 2):
            raise ValueError("invalid workstream reservation")
        with self.locked() as state:
            self.check(state)
            if job_id in state["active"]:
                raise PolicyStop("job already holds a workstream reservation")
            seconds, attempts = float(requested_seconds), max_attempts
            reservations = list(state["active"].values())
            if self.tier == "local" and self.kind == "repair":
                remaining_attempts = (REPAIR_ATTEMPTS - state["repair_attempts"]
                    - sum(item.get("reserved_attempts", 0) for item in reservations
                          if item.get("kind") == "repair"))
                remaining_seconds = (REPAIR_SECONDS - state["repair_seconds"]
                    - sum(item.get("reserved_seconds", 0) for item in reservations
                          if item.get("kind") == "repair"))
                if remaining_attempts <= 0 or remaining_seconds <= 0:
                    raise PolicyStop("repair allowance reserved by another job; no parallel renewal",
                                     "escalate_cloud")
                attempts = min(attempts, remaining_attempts)
                seconds = min(seconds, remaining_seconds)
            if self.tier == "cloud":
                cloud_reservations = [item for item in reservations if item.get("tier") == "cloud"]
                remaining_attempts = (CLOUD_ATTEMPTS - state["cloud_attempts"]
                    - sum(item.get("reserved_attempts", 0) for item in cloud_reservations))
                remaining_seconds = (CLOUD_SECONDS - state["cloud_seconds"]
                    - sum(item.get("reserved_seconds", 0) for item in cloud_reservations))
                if remaining_attempts <= 0 or remaining_seconds <= 0:
                    raise PolicyStop("cloud allowance is reserved by another job", "needs_user")
                attempts = min(attempts, remaining_attempts)
                seconds = min(seconds, remaining_seconds)
            state["active"][job_id] = dict(kind=self.kind, tier=self.tier,
                reserved_attempts=attempts if (self.kind == "repair" or self.tier == "cloud") else 0,
                reserved_seconds=seconds if (self.kind == "repair" or self.tier == "cloud") else 0,
                recorded=0, started=0, pid=os.getpid())
            return dict(job_budget_seconds=seconds, max_attempts=attempts,
                        empty_streak=state["empty_streak"], state_path=str(self.path), tier=self.tier)

    def before_attempt(self, job_id):
        with self.locked() as state:
            self.check(state)
            if job_id not in state["active"]:
                raise PolicyStop("workstream reservation missing")
            claim = state["active"][job_id]
            if (claim.get("reserved_attempts", 0)
                    and claim.get("started", claim["recorded"]) >= claim["reserved_attempts"]):
                raise PolicyStop("workstream attempt allowance exhausted",
                                 "needs_user" if self.tier == "cloud" else "escalate_cloud")

    def start_attempt(self, job_id):
        """Charge inference admission durably even if its runner never returns."""
        with self.locked() as state:
            self.check(state)
            claim = state["active"].get(job_id)
            if claim is None:
                raise PolicyStop("workstream reservation missing")
            if (claim.get("reserved_attempts", 0)
                    and claim.get("started", 0) >= claim["reserved_attempts"]):
                raise PolicyStop("workstream attempt allowance exhausted",
                                 "needs_user" if self.tier == "cloud" else "escalate_cloud")
            claim["started"] = claim.get("started", 0) + 1

    def record_attempt(self, job_id, *, capability_failure: bool, has_patch: bool) -> dict:
        if not isinstance(capability_failure, bool) or not isinstance(has_patch, bool):
            raise ValueError("capability_failure and has_patch must be booleans")
        with self.locked() as state:
            if job_id not in state["active"]:
                raise PolicyStop("workstream reservation missing")
            claim = state["active"][job_id]
            claim["started"] = max(claim.get("started", 0), claim["recorded"] + 1)
            claim["recorded"] += 1
            # Environment failures interrupt a consecutive capability-failure streak but do
            # not contribute to it. A passing/capable attempt also clears the streak.
            state["empty_streak"] = state["empty_streak"] + 1 if capability_failure else 0
            return dict(empty_streak=state["empty_streak"], capability_failure=capability_failure,
                        has_patch=bool(has_patch), tier=self.tier)

    def finish(self, job_id, elapsed_seconds):
        with self.locked() as state:
            claim = state["active"].pop(job_id, None)
            if claim:
                elapsed = max(0.0, float(elapsed_seconds))
                started = claim.get("started", claim["recorded"])
                if self.tier == "local" and claim["kind"] == "repair":
                    state["repair_attempts"] += started
                    state["repair_seconds"] += elapsed
                if self.tier == "cloud":
                    state["cloud_attempts"] += started
                    state["cloud_seconds"] += elapsed


def reset(state_dir, repo_identity, name, actor: str) -> dict:
    """Reset both tier budgets for one stable workstream and record who requested it."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("workstream must be a stable 1-64 character name, without paths")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    root = Path(state_dir) / "workstreams"
    root.mkdir(parents=True, exist_ok=True)
    reset_files = []
    for tier in ("local", "cloud"):
        item = Workstream(state_dir, repo_identity, name, tier=tier)
        with item.locked() as state:
            if state["active"]:
                raise PolicyStop("cannot reset a workstream with active job reservations")
            state.update(empty_streak=0, repair_attempts=0, repair_seconds=0.0,
                         cloud_attempts=0, cloud_seconds=0.0)
            reset_files.append(str(item.path))
    result = {"reset": True, "repo_identity": str(repo_identity), "workstream": name,
              "actor": actor.strip(), "tiers": ["local", "cloud"], "state_files": reset_files}
    with (root / "reset.log").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(), **result),
                                sort_keys=True) + "\n")
    return result
