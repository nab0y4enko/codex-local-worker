#!/usr/bin/env python3
"""Local-first implementation worker: drive one Codex CLI worker against a local oMLX model.

Standard library only. Design rules:

* Nothing about the model is hardcoded: every job asks the live oMLX server which chat model
  is loaded and what its limits are (``omlx_discovery``), then pins exactly that model, its
  endpoint and its context window. The endpoint is loopback only; proxies and redirects are
  refused, and the server must have model fallback disabled. The server is re-read before every
  attempt: if the pinned model is gone or changed, that attempt does not start.
* The oMLX key is loaded into the child environment only; it is excluded from the
  model's shell environment via shell_environment_policy and redacted from all logs.
* Local jobs run Codex in an empty per-job ``CODEX_HOME`` inside the report directory. Cloud
  jobs use the selected native ``CODEX_HOME`` for Codex-managed authentication and refresh, with
  the deny profile allowing metadata on the home directory, ``auth.json``, and its observed
  temporary helper subtree. Both tiers run in ephemeral sessions; local jobs use a model catalog
  generated for the discovered model (``codex_catalog``) and kept in the report directory, which
  the worker cannot write. Local workers never see the user's Codex home, and the self-check pins
  the catalog file's hash. An installed Codex that is too old or lacks a required flag is a
  preflight refusal.
* Every job runs in its own detached ``git worktree``. The primary checkout is never
  mutated. Failed worktrees are preserved, never auto-merged, never committed.
* One job runs at a time by default. ``--max-workers`` 2-4 widens that to a bounded pool of
  exclusive slot leases in the same shared state directory, always mutually exclusive with a
  default job and always refused when owned paths of an active same-repository contract
  overlap. There is no fork scheduler: the planner starts every job itself.
* Post-run audit *detects* out-of-scope edits; it does not prevent them. Execution is
  full-host minus a short deny list: the worker, every verification command and every native
  step run as ``sandbox-exec -f <report_dir>/permissions.sb``, a permissive Seatbelt profile
  (``allow default`` plus denials) generated from the user's ``<state>/permissions.json``
  (``permissions``). The listed secrets cannot be read; the primary checkout's files and its
  git directory (wherever it lives; only ``objects/`` stays writable) and the launcher's own
  state cannot be written. Everything else the
  launcher can read or write stays reachable. Where ``sandbox-exec`` is missing or cannot apply
  the profile, jobs run without it and the report says ``deny_list_applied: false``.
* Independent verification runs the contract commands on the host inside that wrapper. Every
  result is logged truthfully with ``sandboxed: false`` (a deny list is not a sandbox) and
  ``deny_list_applied``; a command that could not be run is reported blocked with the reason it
  was not run.

All guarantees apply only to jobs launched through this launcher.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import apple_verification as apple
import classify as failure_classify
import contract
import codex_catalog
import omlx_discovery
import permissions
import read_answers
import worker_pool as pool
import native_readiness as native
import worker_observability as observation
import run_policy as policy
import worker_tools as tools
from worker_tool_probe import ToolProbeError, probe_tools

_RUN_OBSERVER = contextvars.ContextVar("local_worker_observer", default=None)

# --------------------------------------------------------------------------------------
# Fixed local policy
# --------------------------------------------------------------------------------------
# oMLX serves its OpenAI-compatible API under this prefix; host and port come from discovery.
API_PREFIX = "/v1"
PROVIDER_ID = "local-omlx"
API_KEY_ENV = "LOCAL_OMLX_API_KEY"

OMLX_DIR = Path("~/.omlx")
OMLX_SETTINGS = OMLX_DIR / "settings.json"

# Canonical environment names; the pre-rename names stay honored as legacy fallbacks so an
# existing shell profile keeps working without re-exporting anything.
STATE_ENV = "LOCAL_WORKER_STATE_DIR"
CODEX_BIN_ENV = "LOCAL_WORKER_CODEX_BIN"
LEGACY_STATE_ENV = "LOCAL_IMPLEMENT_STATE_DIR"
LEGACY_CODEX_BIN_ENV = "LOCAL_IMPLEMENT_CODEX_BIN"
# Deliberate legacy storage-compatibility exception, not an oversight: the state root keeps the
# historical ``~/.local-implement`` path on purpose. That directory IS the shared pool - the
# worker/coordinator locks, ``slots/``, ``leases/``, ``reports/`` history and preserved
# ``worktrees/`` all live there - so a new name would silently split one pool into two roots and
# let two jobs believe they own it. Only the *names around* the storage were renamed.
DEFAULT_STATE_DIR = "~/.local-implement"

MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 14400
ALLOWED_ATTEMPTS = (1, 2)
LOCK_TIMEOUT_SECONDS = 30.0
PROBE_MARKER = "__LOCAL_WORKER_SANDBOX_OK__"
PATCH_REPORT_BYTES = 262144
# I/O chunk size when fingerprinting files; unrelated to any model.
READ_CHUNK_BYTES = 1 << 16
TERMINATE_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 10.0
MIN_ATTEMPT_BUDGET_SECONDS = 5.0

# Default execution backend: the worker and every verification command run with the
# launcher's own host permissions and network access, minus the job's deny wrapper when it is
# applied. The report says which, instead of claiming a confinement that is not applied.
FULL_HOST_BACKEND = "full-host-access"
# No confining profile: the command is the contract argv (behind the deny wrapper, if applied).
FULL_HOST_PROFILE = "none"
STALL_FLOOR_SECONDS = 120.0
STALL_UNKNOWN_RATES_SECONDS = 900.0
STALL_SAFETY = 1.5
FULL_HOST_PROBE_ARGV = ["/bin/echo", PROBE_MARKER]
FULL_HOST_CONFINEMENT = "none (full host: launcher permissions, network enabled)"
FULL_HOST_DENY_CONFINEMENT = ("full host minus the permissions deny list (sandbox-exec deny "
                              "profile: secrets unreadable, primary checkout and launcher state "
                              "unwritable), network enabled")
# The job's deny wrapper (spec §6.2, §6.14): the profile generated per job in its report
# directory, the trivial command that proves it can be applied before anything runs under it,
# and the ``sandbox-exec`` exit status (EX_OSERR) that means it could not be applied here.
PERMISSIONS_PROFILE_NAME = "permissions.sb"
WRAPPER_PROBE_ARGV = ["/usr/bin/true"]
WRAPPER_NOT_APPLIED_EXIT = 71
# Report-directory paths a wrapped process may write besides its worktree and Codex home: the
# native steps' scratch (derived data, result bundles) and screenshot captures.
NATIVE_SCRATCH_NAME = "apple-scratch"
SCREENSHOTS_DIR_NAME = "screenshots"
PROBE_TIMEOUT_SECONDS = 60.0
# Reported with every job so a reviewer always knows what confined the verification run.
# ``--context-cap`` only ever lowers the server's window; these bound what a caller may ask for.
MIN_CONTEXT_CAP = 4096
MAX_CONTEXT_CAP = 1048576
# The worker compacts its own context at this share of the pinned window, so Codex compacts
# before the server rejects a request that no longer fits.
COMPACT_FRACTION = 0.8

# Every launcher-driven git subprocess disables inherited hooks/fsmonitor and refuses
# interactive editors, so a cloned repository cannot run host-side hook executables.
GIT_GLOBAL_ARGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.attributesFile=/dev/null",
    "--no-pager",
)
GIT_SAFE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_EDITOR": "/usr/bin/false",
    "GIT_SEQUENCE_EDITOR": "/usr/bin/false",
}
# Literal ``-c key=value`` overrides that must be present in the worker argv whatever model is
# loaded. The worker runs with --ignore-user-config, so these pins plus the job's profile pins
# (``policy_pins``: model, endpoint, context window, compaction) are the whole policy: no
# approval prompts, no web search, no provider retries (a retry could mask a routing problem),
# and the local key excluded from model-generated shell commands.
CONFIG_PINS = {
    "model_provider": f'"{PROVIDER_ID}"',
    f"model_providers.{PROVIDER_ID}.request_max_retries": "0",
    f"model_providers.{PROVIDER_ID}.stream_max_retries": "0",
    "approval_policy": '"never"',
    "web_search": '"disabled"',
    "sandbox_mode": '"danger-full-access"',
    "shell_environment_policy.inherit": '"core"',
}

PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "GATEWAY_INTERFACE",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "CURL_HOME",
)
# ``CODEX_HOME`` is deliberately not inherited: every job sets its own (``job_codex_home``).
BASE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR",
    "TMP", "TEMP", "TZ", "XPC_SERVICE_NAME",
)
CLOUD_ENV_PREFIXES = (
    "OPENAI_", "ANTHROPIC_", "AZURE_OPENAI", "GOOGLE_", "GEMINI_", "VERTEX_", "AWS_",
    "S3_", "AZURE_", "XAI_", "GROQ_", "DEEPSEEK_", "MISTRAL_", "COHERE_", "TOGETHER_",
    "HF_", "HUGGING", "GITHUB_", "GH_", "GITLAB_", "NVIDIA_", "AI21_", "TOGETHERAI_",
    "SERPER_", "BRAVE_", "PERPLEXITY_", "W&B", "WANDB_", "SNOWFLAKE_", "DATABRICKS_",
)
CLOUD_ENV_NAMES = (
    "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN", "HUGGINGFACE_TOKEN",
    "XAI_API_KEY", "GROQ_API_KEY",
)

# ``goals`` would offer get/create/update_goal to every worker. There is no
# ``skip_host_skill_discovery`` pin: in Codex 0.164 that under-development flag isolated nothing
# (the host skills were still listed) and only added an error item to every run.
FEATURE_FLAGS = {
    "features.apps": False,
    "features.plugins": False,
    "features.remote_plugin": False,
    "features.multi_agent": False,
    "features.goals": False,
}
# The worker never sees the host's skills. Codex 0.164 discovers ``~/.agents/skills`` whatever
# ``CODEX_HOME`` is, and no switch turns that discovery off, so the skills list is kept out of
# the request entirely and the bundled system skills are never installed into the job home.
SKILL_PINS = {
    "skills.include_instructions": "false",
    "skills.bundled.enabled": "false",
}
# Non-negotiable CLI flags of the worker invocation. The worker never loads the user's config
# or rules, so these flags plus the ``-c`` pins are the entire policy: full-host access,
# machine-readable events, no interactive approval and no persisted session can never be
# inherited by accident.
REQUIRED_WORKER_FLAGS = ("--ignore-user-config", "--ignore-rules", "--json", "--ephemeral")
# The oldest Codex CLI the worker invocation was qualified on, and the ``codex exec`` flags it
# relies on; ``codex_compat`` checks both before any job (or preflight) proceeds.
MIN_CODEX_VERSION = (0, 164, 0)
CODEX_REQUIRED_FLAGS = ("--ephemeral", "--output-schema", "--ignore-user-config",
                        "--ignore-rules", "--json")
CODEX_COMPAT_TIMEOUT_SECONDS = 30.0
# Each job's Codex home is this directory inside its report directory. It is the worker's own
# writable home, so the generated model catalog (``-c model_catalog_json``) is kept beside it in
# the report directory instead, where only the launcher can write.
CODEX_HOME_NAME = "codex-home"
CATALOG_PIN_KEY = "model_catalog_json"
# Single-valued flags: a second, different value is drift, not a later value quietly winning.
SANDBOX_FLAGS = ("-s", "--sandbox")
MODEL_FLAGS = ("-m", "--model")
REQUIRED_SANDBOX_MODE = "danger-full-access"
# Never let the local key, or any credential that leaked into the worker env, reach a
# model-generated shell command.
SHELL_ENV_EXCLUDES = (
    API_KEY_ENV, "OMLX_API_KEY", "OPENAI_API_KEY", "*API_KEY", "*_API_KEY", "*API_TOKEN",
    "*AUTH_TOKEN", "*ACCESS_KEY", "*SECRET*", "*PASSWORD*", "*CREDENTIAL*", "*.pem",
)

EXIT_CODES = {
    "ready": 0,
    "ok": 0,
    "contract_invalid": 1,
    "preflight_failed": 2,
    "blocked": 3,
    "worker_failed": 4,
    "audit_violation": 5,
    "tests_failed": 6,
    "verification_blocked": 7,
    "timeout": 8,
    "model_mismatch": 9,
    "no_changes": 10,
    "interrupted": 11,
    "answer_invalid": 12,
}

LIMITATIONS = (
    "The allowed-path audit runs after the worker finishes: it detects out-of-scope "
    "edits, it cannot prevent them while the worker runs.",
    "Execution is full-host minus a deny list: the worker, every verification command and "
    "every native step run with the launcher's own permissions and network access inside a "
    "permissive macOS Seatbelt profile (allow default plus denials) generated from "
    "<state>/permissions.json. The listed secrets cannot be read, and the primary checkout's "
    "files, HEAD, index, every ref, hooks and linked-worktree admin redirects, the launcher's "
    "state directory, and the deny_write persistence vectors (~/.gitconfig, shell rc files, "
    "~/Library/LaunchAgents by default) cannot be written. It is a deny list, not a sandbox: "
    "anything else the host can read or write (most of the home directory, caches, the "
    "network) stays reachable by a job. Where sandbox-exec is missing or cannot apply the "
    "profile, jobs still run and the report says permissions.deny_list_applied: false, and a "
    "primary-checkout change then fails the job again.",
    "The launcher and skill do not enforce model choice app-wide. Only jobs started "
    "through this launcher are covered; keep the app planner configuration untouched.",
    "Reports record patch evidence, not semantic review. The selected planner model "
    "must still review the diff and run its own checks.",
    "Git subprocesses disable inherited hooks (core.hooksPath=/dev/null), fsmonitor and "
    "interactive editors, and diff runs with --no-ext-diff --no-textconv. Beyond that the "
    "repository is trusted input: custom clean/smudge filters or unusual git config in a "
    "cloned repository can still influence launcher git commands, so only point this "
    "launcher at repositories you trust.",
    "The audit compares the worktree with the original base commit, so committed, staged, "
    "unstaged and untracked work is all visible; a worker that moves HEAD is reported as a "
    "policy violation and its commits are preserved for review, never reset or hidden.",
    "The bounded pool caps concurrency with OS file locks in one shared state directory; "
    "it is not a mergeability guarantee, and every worker shares the same local oMLX "
    "server, so four workers never mean four times the inference throughput.",
)


class LauncherError(Exception):
    """Base class for failures that carry a report status."""

    status = "worker_failed"


class Interrupted(LauncherError):
    """The launcher received a termination signal while a job was active."""

    status = "interrupted"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap_orphans(state_dir: Path) -> List[Dict[str, Any]]:
    """Stop recorded worker groups only after their launcher died and command identity matches."""
    active_dir = Path(state_dir) / "active"
    killed: List[Dict[str, Any]] = []
    if not active_dir.is_dir():
        return killed
    for record_path in sorted(active_dir.glob("*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            launcher_pid = int(record["launcher_pid"])
            pgid = int(record["worker_pgid"])
            command = str(record["worker_command"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if launcher_pid <= 0 or pgid <= 0 or not command or _pid_alive(launcher_pid):
            continue
        if not observation._group_exists(pgid):
            with contextlib.suppress(OSError):
                record_path.unlink()
            continue
        try:
            actual = subprocess.run(["ps", "-o", "command=", "-p", str(pgid)],
                                    capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not actual.startswith(command):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline and observation._group_exists(pgid):
            time.sleep(.02)
        if observation._group_exists(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        item = {"job_id": record.get("job_id"), "launcher_pid": launcher_pid,
                "worker_pgid": pgid, "worker_command": command, "action": "killed"}
        killed.append(item)
        with contextlib.suppress(OSError):
            record_path.unlink()
        try:
            with (active_dir / "reaped.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(item, sort_keys=True) + "\n")
        except OSError:
            pass
    return killed


ContractError = contract.ContractError


class PreflightError(LauncherError):
    """The environment cannot run a worker; ``code`` is the oMLX discovery code when one applies
    and ``candidates`` the model names the caller could choose instead."""

    status = "preflight_failed"

    def __init__(self, message: str = "", code: Optional[str] = None,
                 candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.candidates: Tuple[str, ...] = tuple(candidates)


class CodexIncompatible(PreflightError):
    """The installed Codex CLI cannot run the worker invocation; ``compat`` is the evidence."""

    def __init__(self, compat: Dict[str, Any]) -> None:
        super().__init__("the installed Codex CLI cannot run the worker: %s" % compat["reason"])
        self.compat = dict(compat)


class BlockedError(LauncherError):
    status = "blocked"


class GitError(LauncherError):
    status = "worker_failed"


class DeadlineExpired(LauncherError):
    status = "timeout"


class Deadline:
    """One bounded budget for the whole job: lock, preflight, probe, attempts, audit."""

    def __init__(self, total_seconds: float) -> None:
        self.total_seconds = float(total_seconds)
        self.end = time.monotonic() + self.total_seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def cap(self, seconds: float) -> float:
        return max(0.0, min(float(seconds), self.remaining()))

    def require(self, label: str, minimum: float = MIN_ATTEMPT_BUDGET_SECONDS) -> float:
        left = self.remaining()
        if left < minimum:
            raise DeadlineExpired(
                f"{label}: only {left:.1f}s of the {self.total_seconds:.0f}s total job "
                f"budget remains, below the {minimum:.1f}s minimum"
            )
        return left

    def exceeded(self) -> bool:
        return self.remaining() < 0.0


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def redact(text: str, secrets: Sequence[str]) -> str:
    """Remove secret material from any text before it is persisted or printed."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return text


# The task's public name stays available for callers of the v1 API.
Task = contract.Contract


# --------------------------------------------------------------------------------------
# Loopback endpoint + local provider validation
# --------------------------------------------------------------------------------------
# Every loopback host oMLX may listen on, as it is written inside a URL (IPv6 in brackets).
_LOOPBACK_URL_HOSTS = "|".join(
    re.escape(f"[{host}]" if ":" in host else host) for host in omlx_discovery.LOOPBACK_HOSTS)
ENDPOINT_RE = re.compile(rf"http://(?:{_LOOPBACK_URL_HOSTS}):([0-9]{{1,5}}){re.escape(API_PREFIX)}/?")


def validate_endpoint(endpoint: Any) -> str:
    """Accept only a loopback oMLX endpoint (``http://<loopback host>:<port>/v1``), without a
    trailing slash; anything else is a hard refusal."""
    match = ENDPOINT_RE.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        raise PreflightError(
            f"endpoint must be http://<{'|'.join(omlx_discovery.LOOPBACK_HOSTS)}>:<port>"
            f"{API_PREFIX}, got {endpoint!r}")
    return endpoint.rstrip("/")


def profile_endpoint(profile: omlx_discovery.ServerProfile) -> str:
    """The OpenAI-compatible endpoint of a discovered server: its base URL plus ``/v1``.

    Everything a launch command contains is derived from this, so a profile whose base URL is
    not loopback can never reach one.
    """
    return validate_endpoint(profile.base_url + API_PREFIX)


def stall_limit(profile: omlx_discovery.ServerProfile, context_window: int) -> Dict[str, Any]:
    """Derive the no-output watchdog from measured server rates, or use the fallback."""
    prefill = profile.prefill_tps
    generation = profile.generation_tps
    output_tokens = min(profile.model.max_output_tokens or 8192, 8192)
    inputs = {"context_window": int(context_window), "prefill_tps": prefill,
              "generation_tps": generation, "max_output_tokens": profile.model.max_output_tokens,
              "prompt_tokens": context_window * .8, "generation_tokens": output_tokens,
              "safety": STALL_SAFETY, "floor_seconds": STALL_FLOOR_SECONDS}
    if (type(prefill) not in (int, float) or prefill <= 0 or
            type(generation) not in (int, float) or generation <= 0):
        return {"seconds": STALL_UNKNOWN_RATES_SECONDS, "basis": "unknown_rates",
                "inputs": dict(inputs, fallback_seconds=STALL_UNKNOWN_RATES_SECONDS)}
    seconds = max(STALL_FLOOR_SECONDS,
                  STALL_SAFETY * (inputs["prompt_tokens"] / float(prefill) +
                                  output_tokens / float(generation)))
    return {"seconds": seconds, "basis": "measured", "inputs": inputs}


def server_busy(settings_path: Path, fetch: omlx_discovery.HttpFetch,
                deadline_at: float) -> bool:
    """Check oMLX activity without letting the production HTTP probe outlive its cutoff.

    Injected legacy fetchers keep the established three-argument call contract. The default
    HTTP implementation accepts a timeout and gets only the time remaining to the attempt
    cutoff; callers set that cutoff no later than the job deadline minus verification reserve.
    """
    remaining = deadline_at - time.monotonic()
    if remaining <= .001:
        return False
    try:
        settings = omlx_discovery.read_server_settings(settings_path)
        remaining = deadline_at - time.monotonic()
        if remaining <= .001:
            return False
        if fetch is omlx_discovery.http_fetch:
            code, body = fetch(settings, "/api/status", True, timeout=remaining)
        else:
            code, body = fetch(settings, "/api/status", True)
        if code != 200 or not isinstance(body, dict):
            return False
        return any(type(body.get(name)) is int and body[name] > 0
                   for name in ("active_requests", "waiting_requests"))
    except Exception:
        return False


def strip_proxy_env(env: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    removed = sorted(name for name in env if name in PROXY_ENV_VARS)
    cleaned = {name: value for name, value in env.items() if name not in PROXY_ENV_VARS}
    return cleaned, removed


def cloud_credential_names(env: Dict[str, str]) -> List[str]:
    names = set(CLOUD_ENV_NAMES)
    return sorted(
        name for name in env
        if name in names or name.upper().startswith(CLOUD_ENV_PREFIXES)
    )


# --------------------------------------------------------------------------------------
# Live discovery: the loaded model, its endpoint and its limits (nothing is a constant)
# --------------------------------------------------------------------------------------
def validate_context_cap(value: Any) -> int:
    """An integer token count the discovered window may be capped at (CLI digits allowed)."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) \
            or not MIN_CONTEXT_CAP <= value <= MAX_CONTEXT_CAP:
        raise ContractError(f"context cap must be an integer between {MIN_CONTEXT_CAP} "
                            f"and {MAX_CONTEXT_CAP} tokens, got {value!r}")
    return value


def _discovery_failure(error: omlx_discovery.DiscoveryError) -> PreflightError:
    """A discovery refusal as a launcher refusal that keeps its code and candidates."""
    return PreflightError(str(error), code=error.code, candidates=error.candidates)


def read_api_key(settings_path: Path = OMLX_SETTINGS) -> str:
    """The oMLX key from ``settings.json`` (never echoed); a settings problem is a preflight
    failure with its discovery code."""
    try:
        return omlx_discovery.read_server_settings(settings_path).api_key
    except omlx_discovery.DiscoveryError as error:
        raise _discovery_failure(error) from error


def compact_limit(context_window: int) -> int:
    """The token count at which the worker compacts its own context."""
    return int(context_window * COMPACT_FRACTION)


def policy_pins(profile: omlx_discovery.ServerProfile, context_window: int) -> Dict[str, str]:
    """Every pin that comes from the job's profile; the self-check requires these exact values."""
    return {
        "model": f'"{profile.model.api_model}"',
        f"model_providers.{PROVIDER_ID}.base_url": f'"{profile_endpoint(profile)}"',
        "model_context_window": str(context_window),
        "model_auto_compact_token_limit": str(compact_limit(context_window)),
    }


def load_requested_model(settings_path: Path, model: str,
                         fetch: omlx_discovery.HttpFetch,
                         timeout: Optional[float] = None) -> Dict[str, Any]:
    """Explicitly ask oMLX to load ``model`` (``--load``); evidence for the report."""
    try:
        if timeout is not None and fetch is omlx_discovery.http_fetch:
            omlx_discovery.load_model(Path(settings_path), model, fetch=fetch, timeout=timeout)
        else:
            omlx_discovery.load_model(Path(settings_path), model, fetch=fetch)
    except omlx_discovery.DiscoveryError as error:
        raise _discovery_failure(error) from error
    return {"requested": model, "loaded": True}


def discover_job_profile(settings_path: Path, requested_model: Optional[str],
                         context_cap: Optional[int], fetch: omlx_discovery.HttpFetch
                         ) -> Tuple[omlx_discovery.ServerProfile, int]:
    """Ask the live server what a worker will run on: its profile and the window to pin.

    The window is the server's own, or a lower ``context_cap``; a cap never raises it. A cap that
    is not a valid size is refused before any request is made.
    """
    cap = None if context_cap is None else validate_context_cap(context_cap)
    try:
        profile = omlx_discovery.discover(Path(settings_path), requested_model, fetch=fetch)
    except omlx_discovery.DiscoveryError as error:
        raise _discovery_failure(error) from error
    return profile, profile.effective_context(cap)


def preflight_receipt(profile: omlx_discovery.ServerProfile, context_window: int
                      ) -> Dict[str, Any]:
    """What preflight proved, for the report and the CLI. Never holds the key."""
    return {
        "endpoint": profile_endpoint(profile),
        "profile": profile.as_report(),
        "context_window": context_window,
        "model_fallback": False,
        "api_key": "present (value withheld)",
    }


def run_preflight(settings_path: Path, requested_model: Optional[str],
                  context_cap: Optional[int], fetch: omlx_discovery.HttpFetch) -> Dict[str, Any]:
    """Discover the loaded model and report what a worker would be pinned to."""
    profile, context_window = discover_job_profile(settings_path, requested_model, context_cap,
                                                   fetch)
    return preflight_receipt(profile, context_window)


VERSION_RE = re.compile(r"(?<![0-9.])([0-9]+)\.([0-9]+)\.([0-9]+)")


def _output_text(value: Any) -> str:
    """Process output as text, whether the runner returned bytes or a string."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _run_codex_info(codex_bin: str, args: Sequence[str], run: Callable[..., Any]
                    ) -> Tuple[Optional[Any], Optional[str]]:
    """One bounded informational Codex call: ``(completed process, None)`` or ``(None, why)``."""
    argv = [str(codex_bin), *args]
    try:
        return run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=CODEX_COMPAT_TIMEOUT_SECONDS,
                   check=False), None
    except (OSError, subprocess.SubprocessError) as error:
        return None, "`%s` could not be run (%s)" % (" ".join(argv), error)


def codex_compat(codex_bin: str, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Whether the installed Codex CLI can run the worker invocation, without starting a model.

    Two informational calls: ``codex --version`` must report at least ``MIN_CODEX_VERSION``, and
    ``codex exec --help`` must list every flag in ``CODEX_REQUIRED_FLAGS``. Returns
    ``{"ok", "version", "missing_flags", "reason"}``; ``reason`` is ``None`` only when ok.
    """
    result: Dict[str, Any] = {"ok": False, "version": None, "missing_flags": [], "reason": None}
    minimum = ".".join(str(part) for part in MIN_CODEX_VERSION)
    problems: List[str] = []
    version, failure = _run_codex_info(codex_bin, ["--version"], run)
    if version is None:
        result["reason"] = failure
        return result
    match = VERSION_RE.search(_output_text(version.stdout))
    if version.returncode != 0 or match is None:
        problems.append("`%s --version` did not report a version (exit %s); Codex CLI %s or "
                        "newer is required" % (codex_bin, version.returncode, minimum))
    else:
        found = tuple(int(part) for part in match.groups())
        result["version"] = ".".join(match.groups())
        if found < MIN_CODEX_VERSION:
            problems.append("Codex CLI %s is older than %s, the oldest version this launcher "
                            "supports" % (result["version"], minimum))
    help_run, failure = _run_codex_info(codex_bin, ["exec", "--help"], run)
    if help_run is None:
        problems.append(str(failure))
    else:
        text = _output_text(help_run.stdout) + "\n" + _output_text(help_run.stderr)
        result["missing_flags"] = [
            flag for flag in CODEX_REQUIRED_FLAGS
            if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(flag), text)]
        if result["missing_flags"]:
            problems.append("`codex exec --help` does not list %s"
                            % ", ".join(result["missing_flags"]))
    result["ok"] = not problems
    result["reason"] = "; ".join(problems) or None
    return result


def require_codex_compat(codex_bin: str, run: Callable[..., Any] = subprocess.run
                         ) -> Dict[str, Any]:
    """``codex_compat`` evidence, or a ``CodexIncompatible`` preflight refusal carrying it."""
    compat = codex_compat(codex_bin, run=run)
    if not compat["ok"]:
        raise CodexIncompatible(compat)
    return compat


def job_codex_home(report_dir: Path) -> Path:
    """The local job's own Codex home, ``<report_dir>/codex-home``, empty and private.

    The worker and the tool probe run with ``CODEX_HOME`` set to it, so neither ever reads the
    user's ``AGENTS.md``, memories, skills, sessions or ``auth.json``. A path that already
    exists is refused unless it is an empty real directory: a link or leftover files would
    hand the worker a home the launcher did not make.
    """
    home = Path(report_dir) / CODEX_HOME_NAME
    try:
        home.mkdir(mode=0o700)
    except FileExistsError:
        if home.is_symlink() or not home.is_dir() or any(home.iterdir()):
            raise BlockedError(f"the job Codex home {home} already exists and is not an empty "
                               "directory") from None
    except OSError as error:
        raise BlockedError(f"cannot create the job Codex home {home}: {error}") from error
    home.chmod(0o700)
    return home


def _auth_secret_values(value: Any, key: str = "") -> List[str]:
    secrets = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            secrets.extend(_auth_secret_values(child, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            secrets.extend(_auth_secret_values(child, key))
    elif isinstance(value, str) and len(value) >= 4 and any(
            marker in key.lower() for marker in ("token", "secret", "api_key", "password", "credential")):
        secrets.append(value)
    return secrets


class CloudAuthObservation:
    """Redact credentials after Codex has used its native CODEX_HOME auth store.

    The launcher never copies or writes auth.json. Codex owns its locking and refresh behavior;
    this observer reads token values before execution for redaction and again after the worker
    exits so refreshed values can scrub retained text evidence before the final report persists.
    """

    def __init__(self, source_home: Path, report_dir: Path, secrets: List[str]):
        self.source_home = Path(source_home)
        self.auth_path = self.source_home / "auth.json"
        self.report_dir = Path(report_dir)
        self.secrets = secrets

    def observe_initial(self) -> str:
        """Read token values into the private redaction set before any worker output exists."""
        try:
            self._capture_secrets()
            return "observed"
        except (OSError, ValueError, UnicodeError, BlockedError):
            return "unavailable"

    def _capture_secrets(self) -> None:
        payload = _read_auth_file_nofollow(self.auth_path)
        auth = json.loads(payload.decode("utf-8"))
        if not isinstance(auth, dict):
            raise ValueError("auth.json must contain an object")
        for value in _auth_secret_values(auth):
            if value not in self.secrets:
                self.secrets.append(value)

    def finish(self) -> Dict[str, Any]:
        result = {"credential_scrub": "unavailable", "retained_artifacts_scrubbed": 0}
        try:
            self._capture_secrets()
            result["credential_scrub"] = "observed after Codex exit"
            result["retained_artifacts_scrubbed"] = self._scrub_retained_artifacts()
        except (OSError, ValueError, UnicodeError, BlockedError) as error:
            result["credential_scrub"] = "unavailable (%s)" % error.__class__.__name__
        return result

    def _scrub_retained_artifacts(self) -> int:
        count = 0
        for root, dirs, files in os.walk(str(self.report_dir), followlinks=False):
            dirs[:] = [name for name in dirs
                       if not (Path(root) / name).is_symlink()]
            for name in files:
                path = Path(root) / name
                if path.is_symlink():
                    continue
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                    original = path.read_bytes()
                    decoded = original.decode("utf-8")
                except UnicodeError:
                    continue
                except OSError as error:
                    raise BlockedError("cannot inspect retained cloud evidence (%s)" %
                                       error.__class__.__name__) from None
                scrubbed = redact(decoded, self.secrets)
                if scrubbed != decoded:
                    try:
                        path.write_text(scrubbed, encoding="utf-8")
                        count += 1
                    except OSError as error:
                        raise BlockedError("cannot scrub retained cloud evidence (%s)" %
                                           error.__class__.__name__) from None
        return count


def _read_auth_file_nofollow(path: Path) -> bytes:
    """Read one regular auth file at job completion without following a symlink."""
    if path.is_symlink():
        raise BlockedError("cloud auth file is a symlink")
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise BlockedError("cannot inspect cloud auth file (%s)" % error.__class__.__name__) from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BlockedError("cloud auth file is not a regular file")
        chunks = []
        while True:
            block = os.read(descriptor, READ_CHUNK_BYTES)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        os.close(descriptor)


def resolve_cloud_codex_home(environment: Optional[Dict[str, str]] = None) -> Path:
    """Select the native Codex auth home without reading credential contents."""
    environment = os.environ if environment is None else environment
    configured = environment.get("CODEX_HOME")
    home = Path(configured).expanduser().absolute() if configured else Path.home() / ".codex"
    if home.is_symlink() or not home.is_dir():
        raise BlockedError("cloud CODEX_HOME must be an existing real directory")
    auth_path = home / "auth.json"
    if auth_path.is_symlink():
        raise BlockedError("cloud auth.json must not be a symlink")
    try:
        if not stat.S_ISREG(auth_path.lstat().st_mode):
            raise BlockedError("cloud auth.json must be a regular file")
    except OSError as error:
        raise BlockedError("cloud auth.json is unavailable (%s)" % error.__class__.__name__) from None
    current = home
    for component in ("tmp", "arg0-macos"):
        current = current / component
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise BlockedError("Codex temporary helper path must be a real directory")
    return home


def create_native_roots(report_dir: Path) -> List[Path]:
    """Create ``<report_dir>/apple-scratch`` and ``<report_dir>/screenshots``, private, if absent.

    The deny profile re-opens both for the job and guards each against being renamed, which also
    means a missing one could never be created by a wrapped process. A path that exists but is a
    link or not a directory is refused: the job would be handed something the launcher did not make.
    """
    roots = []
    for name in (NATIVE_SCRATCH_NAME, SCREENSHOTS_DIR_NAME):
        root = Path(report_dir) / name
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            if root.is_symlink() or not root.is_dir():
                raise BlockedError(f"the job directory {root} already exists and is not a "
                                   "directory") from None
        except OSError as error:
            raise BlockedError(f"cannot create the job directory {root}: {error}") from error
        roots.append(root)
    return roots


def model_label(name: str) -> str:
    """A model name the server reported, made safe to quote in a report line."""
    if omlx_discovery.SAFE_MODEL_NAME.fullmatch(name):
        return name
    return repr(name if len(name) <= 80 else name[:80] + "...")


def describe_model_change(profile: omlx_discovery.ServerProfile, settings_path: Path,
                          fetch: omlx_discovery.HttpFetch) -> str:
    """What oMLX reports now, in words, once a recheck found the pinned model gone or changed.

    One more discovery, made only on the way to a refusal: the recheck already reached the
    verdict, so this only words the report and a failure here is described, never raised.
    """
    pinned = profile.model
    try:
        now = omlx_discovery.discover(Path(settings_path), None, fetch=fetch).model
    except omlx_discovery.DiscoveryError as error:
        if error.code == "model_not_loaded":
            return "no chat model is loaded"
        if error.code == "model_ambiguous":
            return "several chat models are loaded (%s)" % ", ".join(
                model_label(name) for name in error.candidates)
        return "oMLX now refuses discovery (%s): %s" % (error.code, str(error)[:200])
    if (now.canonical_id, now.api_model) == (pinned.canonical_id, pinned.api_model):
        if now.context_window < pinned.context_window:
            return "its context window shrank from %d to %d tokens" % (
                pinned.context_window, now.context_window)
        return "it answers with the pinned model again"
    named = model_label(now.api_model)
    if now.canonical_id != now.api_model:
        named += " (%s)" % model_label(now.canonical_id)
    return "it now serves %s" % named


def recheck_pinned_model(profile: omlx_discovery.ServerProfile, settings_path: Path,
                         fetch: omlx_discovery.HttpFetch, attempt_number: int
                         ) -> Optional[Tuple[str, str]]:
    """``None`` while oMLX still serves the model this job pinned, else ``(reason, message)``
    for an attempt that must not start: ``model_changed`` or ``server_unreachable``, with a
    message naming the pinned model and what the server reports now."""
    reason = omlx_discovery.recheck(profile, Path(settings_path), fetch)
    if reason is None:
        return None
    pinned = model_label(profile.model.api_model)
    if reason == "server_unreachable":
        return reason, (
            "before attempt %d oMLX at %s could not be re-read, so the pinned model %s cannot "
            "be confirmed (server unreachable, or settings.json no longer points at it); the "
            "attempt did not start (%s)" % (attempt_number, profile.base_url, pinned, reason))
    return reason, (
        "before attempt %d oMLX no longer serves the model pinned for this job, %s: %s; the "
        "attempt did not start (%s)" % (
            attempt_number, pinned, describe_model_change(profile, settings_path, fetch), reason))


# --------------------------------------------------------------------------------------
# Git helpers (argument arrays only, no shell, no inherited hooks)
# --------------------------------------------------------------------------------------
def git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Never a pager, never an editor, never a terminal prompt, never system attributes."""
    env = dict(os.environ)
    env.update(GIT_SAFE_ENV)
    if extra:
        env.update(extra)
    return env


def git(repo: Path, *args: str, stdin: Optional[bytes] = None, check: bool = True,
        env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run git with hooks/fsmonitor disabled. ``env`` layers on top of the safe defaults."""
    argv = ["git", *GIT_GLOBAL_ARGS, "-C", str(repo), *args]
    process = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=False, env=git_env(env))
    if check and process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip() or process.stdout.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed (exit {process.returncode}): {detail}")
    return process


def rev_parse(repo: Path, revision: str) -> str:
    process = git(repo, "rev-parse", "--verify", "--quiet", revision)
    return process.stdout.decode("utf-8", "replace").strip()


def _parse_porcelain_z(raw: bytes, include_ignored: bool = False) -> List[Tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into (status, path) pairs."""
    records = [chunk for chunk in raw.split(b"\0")]
    entries: List[Tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2].decode("latin-1")
        path = record[3:].decode("utf-8", "surrogateescape")
        if status[0] in ("R", "C") or status[1] in ("R", "C"):
            if index < len(records):
                origin = records[index].decode("utf-8", "surrogateescape")
                index += 1
                entries.append((status, origin))
        if status == "!!" and not include_ignored:
            continue
        entries.append((status, path))
    return entries


def status_entries(repo: Path, *, ignored: bool = False, ignore_submodules: bool = False,
                   env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """``git status`` entries without touching the index (--no-optional-locks).

    ``ignore_submodules`` adds ``--ignore-submodules=all``: git then never runs ``git status``
    inside a submodule or nested repository at a gitlink, which would read (and run) that
    repository's own config.
    """
    argv = ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if ignore_submodules:
        argv.append("--ignore-submodules=all")
    if ignored:
        argv.append("--ignored=matching")
    process = git(repo, *argv, check=not ignored, env=env)
    if process.returncode != 0:
        return []
    return _parse_porcelain_z(process.stdout, include_ignored=ignored)


def _parse_name_status_z(raw: bytes) -> List[Tuple[str, str, Optional[str]]]:
    """Parse ``git diff --name-status -M -z`` into (code, source, destination) triples."""
    fields = raw.split(b"\0")
    parsed: List[Tuple[str, str, Optional[str]]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        code = field.decode("latin-1")[:1]
        if code in ("R", "C"):
            if index + 1 >= len(fields):
                break
            source = fields[index].decode("utf-8", "surrogateescape")
            destination = fields[index + 1].decode("utf-8", "surrogateescape")
            index += 2
            parsed.append((code, source, destination))
        else:
            if index >= len(fields):
                break
            parsed.append((code, fields[index].decode("utf-8", "surrogateescape"), None))
            index += 1
    return parsed


def changed_paths(repo: Path) -> List[str]:
    paths: List[str] = []
    for _status, path in status_entries(repo):
        clean = path.rstrip("/")
        if clean and clean not in paths:
            paths.append(clean)
    return sorted(paths)


def dirty_primary_files(repo: Path) -> List[str]:
    """Every dirty entry in the primary checkout; these must never be touched."""
    return changed_paths(repo)


class NotRegularFile(OSError):
    """A path the launcher needs to read is not a regular file (a FIFO, socket, device, directory)."""


@contextlib.contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """``path`` opened for binary reading, only if it is a regular file the launcher can read now.

    Every file the launcher reads after a worker ran goes through here, because the worker can
    swap a file it may write for something that would otherwise be followed or waited on. The
    open uses ``O_NOFOLLOW`` (a symlink in the last component is ``ELOOP``, never followed, so
    a link to a file the worker may not read reads nothing) and ``O_NONBLOCK`` (opening a FIFO
    with no writer returns at once instead of waiting forever), then ``fstat`` on the opened
    descriptor must say regular file, else ``NotRegularFile``. The check is on the descriptor
    that is read, so there is no gap between the check and the read.
    """
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        yield handle


def read_regular_file(path: Path) -> bytes:
    """The bytes of a regular file (see ``open_regular_file``); ``OSError`` for anything else."""
    with open_regular_file(path) as handle:
        return handle.read()


def _sha256_of_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular_file(path) as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def job_file_sha256(path: Path) -> Optional[str]:
    """The sha256 of a job file as it is now, or ``None`` when it is not a readable regular file.

    A link or FIFO swapped in for it is ``None``, never hashed through, so what the launcher
    compares against a generated file's digest is only ever that file's own content.
    """
    try:
        return _sha256_of_regular_file(path)
    except OSError:
        return None


def _digest_path(path: Path) -> str:
    """Content fingerprint for one path; comparable before/after without holding data."""
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(str(path))
        if path.is_dir():
            return "directory"
        return _sha256_of_regular_file(path)
    except OSError as error:
        return f"unreadable:{error.__class__.__name__}"


def path_state(path: Path) -> str:
    """State fingerprint for one path: contents plus the ``lstat`` mode.

    A deletion is an explicit marker rather than an absent key, so "the worker deleted this"
    and "this never existed" stay distinguishable without holding any file content.
    """
    try:
        mode = os.lstat(str(path)).st_mode & 0o7777
    except OSError:
        return "missing"
    return f"{_digest_path(path)}:{mode:o}"


def primary_fingerprint(repo: Path) -> Dict[str, Any]:
    """HEAD + index + every dirty path *with its contents*.

    Filenames alone are not enough: a file that was already dirty before the job and is
    modified again during the job keeps the same name, so the launcher compares hashes.
    """
    entries: List[List[str]] = []
    contents: Dict[str, str] = {}
    for status, raw_path in status_entries(repo):
        path = raw_path.rstrip("/")
        if not path:
            continue
        entry = [status, path]
        if entry not in entries:
            entries.append(entry)
        contents[path] = _digest_path(repo / path)
    index = git(repo, "--no-optional-locks", "ls-files", "--stage", "-z", check=False)
    return {
        "head": rev_parse(repo, "HEAD"),
        "index_digest": hashlib.sha256(index.stdout or b"").hexdigest(),
        "entries": sorted(entries),
        "contents": dict(sorted(contents.items())),
    }


def resolve_commit(repo: Path, ref: str) -> str:
    process = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    commit = process.stdout.decode("utf-8", "replace").strip()
    if not commit:
        raise ContractError(f"base_ref: {ref!r} does not resolve to a commit in {repo}")
    return commit


def worktree_name(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", task_id)[:40]
    return f"local-worker-{slug}-{_now_stamp()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def add_worktree(repo: Path, commit: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"refusing to reuse an existing worktree path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(destination), commit)


# --------------------------------------------------------------------------------------
# Single-worker lock
# --------------------------------------------------------------------------------------
@contextlib.contextmanager
def job_lock(state_dir: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """One local worker at a time, via fcntl.flock on <state_dir>/worker.lock."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "worker.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (11, 35, 13):  # EAGAIN/EWOULDBLOCK variants
                    raise BlockedError(f"cannot acquire worker lock: {error}") from error
                if time.monotonic() >= deadline:
                    raise BlockedError(
                        f"another local worker holds {lock_path}; jobs are serialized and "
                        "this job was not started"
                    ) from error
                time.sleep(0.05)
        handle.seek(0)
        handle.truncate(0)
        handle.write(str(os.getpid()))
        handle.flush()
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------------------------------
# Bounded shared-state concurrency
# --------------------------------------------------------------------------------------
def repo_identity(repo: Path) -> str:
    """Canonical repository identity: linked worktrees of one repository share it."""
    common = ""
    try:
        completed = git(repo, "--no-optional-locks", "rev-parse", "--git-common-dir",
                        check=False)
        if completed.returncode == 0:
            common = completed.stdout.decode("utf-8", "replace").strip()
    except (LauncherError, OSError):
        common = ""
    if common and "\n" not in common:
        candidate = common if os.path.isabs(common) else os.path.join(str(repo), common)
        return pool.canonical_repo_path(candidate)
    return pool.canonical_repo_path(repo)


def validate_max_workers(value: Any) -> int:
    """Refuse anything but an integer 1..4 before a worktree or lock is touched."""
    try:
        return pool.validate_max_workers(value)
    except pool.PoolConfigError as error:
        raise ContractError(str(error)) from error


def parse_max_workers(value: Any) -> int:
    """CLI boundary: digits become an integer, then the same hard validation applies."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        value = int(value.strip())
    return validate_max_workers(value)


@contextlib.contextmanager
def job_lease(options: JobOptions, report: Dict[str, Any], timeout: float,
              job_deadline: Optional[float] = None):
    """Take the pool lease for one job, honestly reported.

    Capacity 1 keeps the legacy exclusive ``worker.lock`` untouched; capacity 2..4 takes
    the shared legacy lock plus one exclusive slot lease in the same state directory, so a
    default job and a parallel job still exclude each other in both directions.

    ``job_deadline`` is the absolute ``Deadline.end`` of this job. The pool bounds the lock
    wait, every later metadata update and the cleanup with it, so lease bookkeeping can never
    reset or extend the one budget ``run_job`` created.
    """
    capacity = validate_max_workers(options.maximum)
    task = options.task
    started = time.monotonic()
    try:
        if capacity == 1:
            with job_lock(options.state_dir, timeout):
                waited = round(time.monotonic() - started, 3)
                report["lease"] = {
                    "mode": "exclusive", "capacity": 1, "slot": None,
                    "legacy_lock": "exclusive", "resource": None, "wait_seconds": waited,
                    "waits": {"legacy": waited}, "pool": str(options.state_dir),
                    "task_id": task.task_id,
                    "repo_identity": pool.canonical_repo_path(task.repo_path),
                }
                yield None
            return
        with pool.lease(options.state_dir, capacity=capacity, task_id=task.task_id,
                        repo_identity=repo_identity(task.repo_path),
                        allowed_paths=list(task.allowed_paths),
                        job_id=report.get("job_id"), base_ref=task.base_ref,
                        lock_timeout=timeout,
                        simulator_udid=options.apple_simulator_udid,
                        job_deadline=job_deadline) as holder:
            report["lease"] = holder.as_report()
            yield holder
    except pool.PoolConflictError as error:
        raise BlockedError(str(error)) from error
    except pool.PoolUnavailableError as error:
        raise BlockedError(str(error)) from error
    except pool.PoolConfigError as error:
        raise ContractError(str(error)) from error


def lease_update(lease: Optional["pool.Lease"], **values: Any) -> None:
    """Record lease metadata the parent owns; the slot lock stays held either way."""
    if lease is None:
        return
    try:
        lease.update(**values)
    except pool.PoolError as error:
        raise BlockedError(f"cannot record lease metadata: {error}") from error


# --------------------------------------------------------------------------------------
# Post-run audit: detection, not prevention
# --------------------------------------------------------------------------------------
@dataclass
class Audit:
    changed_paths: List[str] = field(default_factory=list)
    entries: List[Dict[str, str]] = field(default_factory=list)
    violations: List[Dict[str, str]] = field(default_factory=list)
    ignored_paths: List[str] = field(default_factory=list)
    patch: bytes = b""
    diffstat: str = ""
    base_commit: str = ""
    head_commit: str = ""
    head_changed: bool = False


def is_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    for allowed in allowed_paths:
        allowed = allowed.rstrip("/")
        if not allowed:
            continue
        if path == allowed or path.startswith(allowed + "/"):
            return True
    return False


def symlink_escape(worktree: Path, relative: str) -> Optional[str]:
    """Return the offending component when a path leaves the worktree via a symlink."""
    root = os.path.realpath(str(worktree))
    cursor = root
    parts = [part for part in relative.split("/") if part]
    for depth, part in enumerate(parts, start=1):
        cursor = os.path.join(cursor, part)
        if os.path.islink(cursor):
            target = os.path.realpath(cursor)
            if target != root and not target.startswith(root + os.sep):
                return "/".join(parts[:depth])
    full = os.path.realpath(os.path.join(root, *parts)) if parts else root
    if full != root and not full.startswith(root + os.sep):
        return relative
    return None


DIFF_ARGS = ("--no-ext-diff", "--no-textconv", "--find-renames")


def base_gitlinks(worktree: Path, base_commit: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The paths that are gitlinks (mode 160000: submodules) in the base commit's tree, sorted.

    Nothing under one is the job's to change, and git must not look inside one while the
    launcher audits the worktree: a repository the worker made there would have its own config
    (a clean filter, ...) run by the launcher's git. The audit leaves these paths out.
    """
    process = git(worktree, "ls-tree", "-r", "-z", "--full-tree", base_commit, env=env)
    links = []
    for record in (process.stdout or b"").split(b"\0"):
        meta, _, path = record.partition(b"\t")
        if meta.startswith(b"160000 ") and path:
            links.append(path.decode("utf-8", "surrogateescape"))
    return sorted(links)


class _GitRedirected(Exception):
    """A git redirect file differs from its pre-worker state; the audit must not run git."""

    def __init__(self, changed: Sequence[str]) -> None:
        super().__init__(", ".join(changed))
        self.changed = list(changed)


def audit_scratch_dir(worktree: Path, scratch_dir: Optional[Path]) -> Path:
    """Where the throwaway audit index lives; it must not sit inside the worktree."""
    candidate = Path(scratch_dir) if scratch_dir is not None else worktree.parent
    if _inside(candidate, worktree):
        candidate = Path(tempfile.gettempdir())
    if _inside(candidate, worktree):
        raise BlockedError("no scratch directory outside the worktree is available for the audit")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def audit_worktree(worktree: Path, base_commit: str, allowed_paths: Sequence[str],
                   secrets: Sequence[str],
                   scratch_dir: Optional[Path] = None,
                   git_layout: Optional[Dict[str, str]] = None,
                   git_admin_before: Optional[Dict[str, str]] = None) -> Audit:
    """Audit everything that differs from the *original* base commit.

    The comparison target is the commit the job started from, never the moving HEAD, so
    committed, staged, unstaged, deleted, renamed (both sides) and untracked work are all
    visible. Staging happens in a throwaway ``GIT_INDEX_FILE``: the worktree index and
    HEAD are never mutated, and a worker's commits are reported instead of reset.

    ``git_layout`` pins ``GIT_DIR``/``GIT_COMMON_DIR``/``GIT_WORK_TREE`` (captured before the
    worker ran) on every git call here, so a worker that repointed the worktree's ``.git``
    gitfile or its ``commondir`` cannot steer the launcher's own (unwrapped) git at a repository
    it controls, whose config or attributes would otherwise run arbitrary filters. Pinning does
    not cover every redirect (``<admin>/config.worktree`` is still read with
    ``extensions.worktreeConfig``), so ``git_admin_before`` -- the redirect fingerprint from before
    the worker ran -- is compared with the live files before *each* git call: on a difference the
    audit runs no more git, and records the violation instead.

    Gitlinks present in the base tree are left out of staging (a pathspec exclusion) and of
    ``status`` (``--ignore-submodules=all``), so git never descends into a nested repository the
    worker may have planted at one; a change to such a path is therefore not reported.
    """
    layout = dict(git_layout or {})
    audit = Audit(base_commit=base_commit)
    scratch = audit_scratch_dir(worktree, scratch_dir)
    index_path = scratch / f"audit-index-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    audit_env = dict(layout, GIT_INDEX_FILE=str(index_path))
    entries: List[Dict[str, str]] = []
    seen = set()

    def record(status: str, raw_path: str, role: str = "") -> None:
        path = raw_path.rstrip("/")
        if not path:
            return
        key = (status, path, role)
        if key in seen:
            return
        seen.add(key)
        entry = {"status": status, "path": path}
        if role:
            entry["role"] = role
        entries.append(entry)

    def untampered() -> None:
        """Called before every git command: the redirect files must be as they were."""
        if git_admin_before and layout:
            changed = git_redirect_changes(git_admin_before, layout, worktree)
            if changed:
                raise _GitRedirected(changed)

    def run_git(*args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        untampered()
        return git(worktree, *args, env=env)

    def status(**options: Any) -> List[Tuple[str, str]]:
        untampered()
        # status compares the working tree with its *own* index, so it must not see the throwaway
        # staging index; it still gets the pinned git layout, never a tampered gitfile/commondir.
        return status_entries(worktree, ignore_submodules=True, env=(layout or None), **options)

    try:
        audit.head_commit = run_git("rev-parse", "--verify", "--quiet", "HEAD",
                                    env=layout or None).stdout.decode("utf-8", "replace").strip()
        if audit.head_commit != base_commit:
            audit.head_changed = True
            audit.violations.append({
                "path": ".",
                "reason": "worker moved HEAD (git commit is forbidden); commits are preserved "
                          f"for review, not hidden (base {base_commit[:12]} -> "
                          f"head {audit.head_commit[:12]})",
            })

        run_git("read-tree", base_commit, env=audit_env)
        untampered()
        excluded = [f":(exclude,literal){path}"
                    for path in base_gitlinks(worktree, base_commit, env=layout or None)]
        run_git("add", "-A", *(["--", ".", *excluded] if excluded else []), env=audit_env)
        for entry_status, path in status():
            record(entry_status, path)
        name_status = run_git("--no-optional-locks", "diff", "--cached", "--name-status",
                              "-z", *DIFF_ARGS, base_commit, env=audit_env)
        for code, source, destination in _parse_name_status_z(name_status.stdout or b""):
            if destination is None:
                record(code, source)
            else:
                record(code, source, "rename-source")
                record(code, destination, "rename-destination")
        audit.changed_paths = sorted({entry["path"] for entry in entries})

        for path in audit.changed_paths:
            if not is_allowed(path, allowed_paths):
                audit.violations.append({"path": path, "reason": "outside allowed_paths"})
                continue
            escape = symlink_escape(worktree, path)
            if escape:
                audit.violations.append({"path": path, "reason": f"symlink escape at {escape}"})

        diff = run_git("--no-optional-locks", "diff", "--cached", "--binary",
                       *DIFF_ARGS, base_commit, env=audit_env)
        audit.patch = redact(diff.stdout.decode("utf-8", "replace"), secrets).encode("utf-8")
        summary = run_git("--no-optional-locks", "diff", "--cached", "--stat",
                          *DIFF_ARGS, base_commit, env=audit_env)
        audit.diffstat = redact(summary.stdout.decode("utf-8", "replace"), secrets).strip()

        changed = set(audit.changed_paths)
        ignored = sorted(
            {path.rstrip("/") for entry_status, path in status(ignored=True)
             if entry_status == "!!"}
        )
        audit.ignored_paths = [path for path in ignored if path not in changed]
    except _GitRedirected as redirected:
        audit.changed_paths = sorted({entry["path"] for entry in entries})
        audit.violations.append(git_redirect_violation(redirected.changed, git_skipped=True))
    finally:
        with contextlib.suppress(OSError):
            index_path.unlink()

    audit.entries = sorted(entries, key=lambda item: (item["path"], item["status"],
                                                      item.get("role", "")))
    return audit


# --------------------------------------------------------------------------------------
# Bounded subprocess execution
# --------------------------------------------------------------------------------------
def _group_alive(pgid: int) -> bool:
    """True while any process is still a member of the process group."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_group(process: subprocess.Popen) -> Dict[str, Any]:
    """SIGTERM the group, then *always* SIGKILL it when any member survived the grace.

    Waiting only on the leader is not enough: a descendant that traps SIGTERM keeps the
    group alive after the leader exits. The leader is reaped first (so a finished leader
    is not mistaken for a survivor), then the group itself is probed and killed.
    """
    outcome: Dict[str, Any] = {"terminated": False, "force_killed": False, "survivors": False}
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return outcome
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
        outcome["terminated"] = True
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    if _group_alive(pgid):
        outcome["force_killed"] = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        kill_deadline = time.monotonic() + KILL_GRACE_SECONDS
        while time.monotonic() < kill_deadline:
            # The killed leader is *our* child: until it is reaped its zombie still answers
            # killpg(pgid, 0), so an unreaped leader would look like a survivor forever.
            process.poll()
            if not _group_alive(pgid):
                break
            time.sleep(0.05)
        outcome["survivors"] = _group_alive(pgid)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    return outcome


def run_bounded(argv: Sequence[str], *, cwd: Optional[Path], env: Dict[str, str],
                timeout: float, stdin_data: Optional[bytes] = None, stall_seconds=None,
                on_stall=None) -> Dict[str, Any]:
    """Run argv in its own process group and kill the whole group on deadline.

    One runner does all of it. ``observation.run_streaming`` drains both pipes concurrently so
    output stays bounded by ``CAPTURE_LIMIT_BYTES`` per stream, terminates the whole group on a
    deadline, an interrupt or an over-cap flood, and reports the real exit status together with
    the ``output_limit_exceeded`` flag. The file-backed variant this replaces was a second
    implementation of the same promise *and* a weaker one: an interrupt propagated while a
    descendant still held the redirected temporary file, so an orphaned writer kept growing it
    after ``run_bounded`` returned.
    """
    return observation.run_streaming(
        argv, cwd=cwd, env=env, timeout=timeout, stdin_data=stdin_data,
        observer=_RUN_OBSERVER.get() if stdin_data is not None else None,
        stall_seconds=stall_seconds, on_stall=on_stall,
        terminate_group=_terminate_group)


# --------------------------------------------------------------------------------------
# Worker invocation
# --------------------------------------------------------------------------------------
def catalog_pin(catalog_path: Path) -> str:
    """The ``model_catalog_json`` value for a catalog file: its path as a quoted TOML string."""
    return json.dumps(str(catalog_path))


def config_overrides(profile: omlx_discovery.ServerProfile, context_window: int,
                     catalog_path: Optional[Path] = None,
                     sandbox_mode: str = REQUIRED_SANDBOX_MODE) -> List[str]:
    """Explicit local provider/model overrides for the job's discovered model; the user's
    config is never loaded. ``catalog_path`` is the job's generated model catalog: when given,
    ``model_catalog_json`` points Codex at it, so the model runs on its own metadata instead of
    Codex's fallback."""
    provider = f"model_providers.{PROVIDER_ID}"
    pins = policy_pins(profile, context_window)
    settings = [
        ("model", pins["model"]),
        ("model_provider", f'"{PROVIDER_ID}"'),
        (f"{provider}.name", '"Local oMLX (loopback only)"'),
        (f"{provider}.base_url", pins[f"{provider}.base_url"]),
        (f"{provider}.env_key", f'"{API_KEY_ENV}"'),
        (f"{provider}.wire_api", '"responses"'),
        (f"{provider}.requires_openai_auth", "false"),
        # A retry would mask a routing or availability problem, and a second request could
        # be served by something else: the worker gets exactly one attempt per request.
        (f"{provider}.request_max_retries", "0"),
        (f"{provider}.stream_max_retries", "0"),
        # Unattended and local only: no approvals, no web search, bounded context, and the
        # full-host access that -s selects (pinned so config drift cannot silently change it).
        ("approval_policy", '"never"'),
        ("web_search", '"disabled"'),
        ("sandbox_mode", json.dumps(sandbox_mode)),
        ("model_context_window", pins["model_context_window"]),
        ("model_auto_compact_token_limit", pins["model_auto_compact_token_limit"]),
    ]
    if catalog_path is not None:
        settings.append((CATALOG_PIN_KEY, catalog_pin(catalog_path)))
    for key, value in sorted(FEATURE_FLAGS.items()):
        settings.append((key, "true" if value else "false"))
    settings.extend(sorted(SKILL_PINS.items()))
    settings.append(("shell_environment_policy.inherit", '"core"'))
    settings.append(shell_env_override())
    return [item for key, value in settings for item in ("-c", f"{key}={value}")]


SHELL_ENV_EXCLUDES_KEY = "shell_environment_policy.exclude"


def shell_env_exclude_names(extras: Sequence[str] = ()) -> List[str]:
    """Baseline exclusions first, then every added name, each exactly once."""
    names: List[str] = []
    for name in [*SHELL_ENV_EXCLUDES, *extras]:
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def shell_env_override(extras: Sequence[str] = ()) -> Tuple[str, str]:
    """The one ``shell_environment_policy.exclude`` override for the baseline plus *extras*."""
    members = ",".join(json.dumps(name) for name in shell_env_exclude_names(extras))
    return (SHELL_ENV_EXCLUDES_KEY, f"[{members}]")


def with_shell_env_excludes(argv: Sequence[str], extras: Sequence[str] = ()) -> List[str]:
    """Replace the exclude pin with the union of the baseline and *extras*.

    One override replaces the baseline rather than adding a second ``-c``: two conflicting
    values for the same key would be a self-check refusal, and the last one winning by accident
    is exactly the drift this check exists to prevent. With nothing to add the argv is returned
    untouched, so a job without a tools manifest keeps its exact policy.
    """
    items = list(argv)
    additions = [name for name in extras if isinstance(name, str) and name]
    if not [name for name in additions if name not in SHELL_ENV_EXCLUDES]:
        return items
    key, value = shell_env_override(additions)
    kept: List[str] = []
    index = 0
    while index < len(items):
        if str(items[index]) == "-c" and index + 1 < len(items) \
                and str(items[index + 1]).startswith(key + "="):
            index += 2
            continue
        kept.append(items[index])
        index += 1
    kept.extend(["-c", f"{key}={value}"])
    return kept


def config_override_pairs(argv: Sequence[str]) -> List[Tuple[str, str]]:
    """Every ``-c key=value`` occurrence in argv order, so a duplicate cannot hide drift."""
    pairs: List[Tuple[str, str]] = []
    for index, item in enumerate(argv):
        if item == "-c" and index + 1 < len(argv) and "=" in argv[index + 1]:
            key, _, value = argv[index + 1].partition("=")
            pairs.append((key, value))
    return pairs


def config_overrides_map(argv: Sequence[str]) -> Dict[str, str]:
    """Collect the ``-c key=value`` overrides present in an argv (last occurrence wins)."""
    return dict(config_override_pairs(argv))


def flag_values(argv: Sequence[str], flag: str) -> List[str]:
    """Every value given for *flag* (``-s X`` or ``-s=X``), in argv order."""
    values: List[str] = []
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            continue
        if item == flag and index + 1 < len(argv):
            values.append(str(argv[index + 1]))
        elif item.startswith(flag + "="):
            values.append(item.split("=", 1)[1])
    return values


def _single_valued_flag(argv: Sequence[str], flags: Sequence[str], expected: str) -> List[str]:
    """Problems for a flag that must appear with *expected* as its value across all aliases.

    Aliases are one setting, so every occurrence is collected before anything is validated:
    returning after the first alias that has a value would let ``-m correct --model wrong``
    pass while the winning alias silently routes somewhere else.
    """
    given: List[Tuple[str, str]] = [(flag, value) for flag in flags
                                    for value in flag_values(argv, flag)]
    if not given:
        return [f"{flags[0]} {expected} is missing"]
    distinct = sorted({value for _, value in given})
    if len(distinct) > 1:
        return [f"{flags[0]} is given more than once with conflicting values {distinct} "
                f"(given as {' '.join(f'{flag} {value}' for flag, value in given)})"]
    flag, value = given[0]
    if value != expected:
        return [f"{flag} {expected} got {flag} {value}"]
    return []


def shell_env_excludes(value: Optional[str]) -> List[str]:
    """Member names of the ``shell_environment_policy.exclude`` array, parsed exactly."""
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def expected_config_pins(profile: omlx_discovery.ServerProfile,
                         context_window: int,
                         sandbox_mode: str = REQUIRED_SANDBOX_MODE) -> Dict[str, str]:
    """The full pinned ``-c`` policy, derived from the single place that builds it."""
    return config_overrides_map(config_overrides(profile, context_window,
                                                 sandbox_mode=sandbox_mode))


def missing_config_pins(argv: Sequence[str], profile: omlx_discovery.ServerProfile,
                        context_window: int,
                        catalog: Optional[Tuple[Path, str]] = None,
                        sandbox_mode: str = REQUIRED_SANDBOX_MODE,
                        output_schema: Optional[Path] = None) -> List[str]:
    """Self-check before launching: any drift from the pinned local policy is a refusal.

    ``--ignore-user-config`` means the argv *is* the whole policy, so routing flags and every
    pinned override are checked together: a missing pin, a changed value, a conflicting
    duplicate override or a second model/sandbox flag all refuse the launch instead of letting
    the last occurrence quietly win. Every expected value comes from the job's own pinned
    ``profile`` and ``context_window``, so the argv must name exactly the model, endpoint and
    window discovery found. ``catalog`` is the job's generated model catalog and the sha256
    ``codex_catalog.write_catalog`` returned for it: the argv must point ``model_catalog_json``
    at exactly that file, and the file is hashed again here (``job_file_sha256``, which never
    follows a link or waits on a FIFO), so a catalog changed after it was written is drift too.
    Without ``catalog`` no ``model_catalog_json`` may appear at all.
    """
    alias = profile.model.api_model
    endpoint = profile_endpoint(profile)
    pairs = config_override_pairs(argv)
    overrides = dict(pairs)
    problems: List[str] = []
    pinned = expected_config_pins(profile, context_window, sandbox_mode=sandbox_mode)
    # The documented floor stays required on its own: deleting a pin from config_overrides()
    # must not silently delete the requirement for it.
    for key, expected in {**CONFIG_PINS, **SKILL_PINS,
                          **policy_pins(profile, context_window)}.items():
        pinned.setdefault(key, expected)
    if catalog is not None:
        catalog_path, catalog_sha = catalog
        pinned[CATALOG_PIN_KEY] = catalog_pin(catalog_path)
        actual_sha = job_file_sha256(catalog_path)
        if actual_sha is None:
            problems.append(f"{CATALOG_PIN_KEY} names {catalog_path}, which can no longer be "
                            "read")
        elif actual_sha != catalog_sha:
            problems.append(f"{CATALOG_PIN_KEY} file {catalog_path} changed after it was "
                            f"generated (sha256 {actual_sha}, generated {catalog_sha})")
    elif CATALOG_PIN_KEY in overrides:
        problems.append(f"{CATALOG_PIN_KEY}={overrides[CATALOG_PIN_KEY]} names a catalog this "
                        "job did not generate")
    for key, expected in sorted(pinned.items()):
        actual = overrides.get(key)
        if actual is None:
            problems.append(f"{key}={expected} is missing")
        elif key == SHELL_ENV_EXCLUDES_KEY:
            # Task tools may only *add* their own credential names to the exclusions: removing
            # or replacing a baseline name is drift, and every routing pin stays exact.
            removed = [name for name in shell_env_excludes(expected)
                       if name not in shell_env_excludes(actual)]
            if removed:
                problems.append(f"{key} may only add names to the baseline exclusions; "
                                f"{', '.join(removed)} were removed")
        elif actual != expected:
            problems.append(f"{key}={expected} got {actual}")
    duplicated: Dict[str, List[str]] = {}
    for key, value in pairs:
        duplicated.setdefault(key, []).append(value)
    for key, values in sorted(duplicated.items()):
        if len(set(values)) > 1:
            problems.append(f"-c {key} is overridden more than once with conflicting values "
                            f"{sorted(set(values))}")
    for key, value in pairs:
        if key.endswith(".base_url") and value != f'"{endpoint}"':
            problems.append(f"{key} must stay on the pinned loopback endpoint {endpoint}, "
                            f"got {value}")
    for flag in REQUIRED_WORKER_FLAGS:
        if flag not in argv:
            problems.append(f"{flag} is missing")
    problems.extend(_single_valued_flag(argv, SANDBOX_FLAGS, sandbox_mode))
    problems.extend(_single_valued_flag(argv, MODEL_FLAGS, alias))
    if output_schema is not None:
        if argv.count("--output-schema") != 1:
            problems.append("--output-schema must appear exactly once")
        elif argv[argv.index("--output-schema") + 1:
                  argv.index("--output-schema") + 2] != [str(output_schema)]:
            problems.append("--output-schema must name the generated answer schema")
    inherit = overrides.get("shell_environment_policy.inherit")
    if inherit not in ('"none"', '"core"'):
        problems.append('shell_environment_policy.inherit must be "none" or "core", '
                        f"got {inherit!r}")
    if inherit == '"core"' and API_KEY_ENV not in shell_env_excludes(
            overrides.get("shell_environment_policy.exclude")):
        problems.append(f"shell_environment_policy.exclude must exclude {API_KEY_ENV}")
    return problems


def worker_argv(codex_bin: str, worktree: Path, profile: omlx_discovery.ServerProfile,
                context_window: int, last_message: Path,
                tool_overrides: Sequence[str] = (),
                shell_excludes: Sequence[str] = (),
                catalog_path: Optional[Path] = None,
                sandbox_mode: str = REQUIRED_SANDBOX_MODE,
                output_schema: Optional[Path] = None) -> List[str]:
    """The whole worker command for the job's pinned ``profile`` and ``context_window``."""
    argv = [
        codex_bin, "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "-C", str(worktree),
        "-s", sandbox_mode,
        "--json",
        "--ephemeral",
        "-o", str(last_message),
        "-m", profile.model.api_model,
        *config_overrides(profile, context_window, catalog_path, sandbox_mode=sandbox_mode),
    ]
    argv = with_shell_env_excludes(argv, shell_excludes)
    argv.extend(str(item) for item in tool_overrides)
    if output_schema is not None:
        argv.extend(["--output-schema", str(output_schema)])
    argv.extend(["--", "-"])
    return argv


def cloud_worker_argv(codex_bin: str, worktree: Path, model: str,
                      reasoning_effort: Optional[str], last_message: Path,
                      tool_overrides: Sequence[str] = (),
                      shell_excludes: Sequence[str] = (),
                      sandbox_mode: str = REQUIRED_SANDBOX_MODE,
                      output_schema: Optional[Path] = None) -> List[str]:
    """Cloud invocation with no local provider or endpoint overrides."""
    argv = [codex_bin, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--json", "-C", str(worktree), "-s", sandbox_mode, "-o", str(last_message),
            "-m", model]
    if reasoning_effort is not None:
        argv.extend(["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
    argv = with_shell_env_excludes(argv, shell_excludes)
    argv.extend(str(item) for item in tool_overrides)
    if output_schema is not None:
        argv.extend(["--output-schema", str(output_schema)])
    argv.extend(["--", "-"])
    return argv


def cloud_policy_check(argv: Sequence[str], model: str, reasoning_effort: Optional[str],
                       sandbox_mode: str = REQUIRED_SANDBOX_MODE) -> List[str]:
    """Return cloud argv drift, including any accidental local routing pins."""
    problems = []
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--json"):
        if list(argv).count(flag) != 1:
            problems.append(flag + " must appear exactly once")
    problems.extend(_single_valued_flag(argv, SANDBOX_FLAGS, sandbox_mode))
    problems.extend(_single_valued_flag(argv, MODEL_FLAGS, model))
    overrides = config_override_pairs(argv)
    for key, value in overrides:
        if key.startswith("model_provider") or key.startswith("model_providers."):
            problems.append("cloud argv must not override local provider setting " + key)
        if key == "model_reasoning_effort" and reasoning_effort is not None:
            expected = json.dumps(reasoning_effort)
            if value != expected:
                problems.append("model_reasoning_effort must be " + expected)
    if reasoning_effort is not None and "model_reasoning_effort" not in dict(overrides):
        problems.append("model_reasoning_effort is missing")
    return problems


def cloud_config_overrides(reasoning_effort: Optional[str]) -> List[str]:
    return [] if reasoning_effort is None else ["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)]


def worker_argv_for_tier(options: JobOptions, worktree: Path,
                         profile: omlx_discovery.ServerProfile, context_window: int,
                         last_message: Path, tool_overrides: Sequence[str] = (),
                         shell_excludes: Sequence[str] = (),
                         catalog_path: Optional[Path] = None,
                         sandbox_mode: str = REQUIRED_SANDBOX_MODE,
                         output_schema: Optional[Path] = None) -> List[str]:
    if options.task.tier.kind == "cloud":
        return cloud_worker_argv(options.codex_bin, worktree, options.task.tier.model or "",
            options.task.tier.reasoning_effort, last_message, tool_overrides, shell_excludes,
            sandbox_mode=sandbox_mode, output_schema=output_schema)
    return worker_argv(options.codex_bin, worktree, profile, context_window, last_message,
        tool_overrides, shell_excludes, catalog_path, sandbox_mode=sandbox_mode,
        output_schema=output_schema)


def cloud_worker_env(codex_home: Path, environment_pass: Sequence[str] = ()) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Minimal cloud environment using Codex's native, narrowly exposed auth home."""
    cleaned, stripped = strip_proxy_env(dict(os.environ))
    cloud = cloud_credential_names(cleaned)
    env = {key: cleaned[key] for key in BASE_ENV_KEYS if key in cleaned}
    env.setdefault("TERM", "dumb")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["CODEX_HOME"] = str(codex_home)
    for name in environment_pass:
        if name in cleaned:
            env[name] = cleaned[name]
    return env, stripped, cloud


def worker_env(api_key: str, codex_home: Path, environment_pass: Sequence[str] = ()
               ) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Minimal child environment: loopback key present, proxies and cloud credentials gone, and
    ``CODEX_HOME`` the job's own home, never the caller's."""
    cleaned, stripped = strip_proxy_env(dict(os.environ))
    cloud = cloud_credential_names(cleaned)
    env = {key: cleaned[key] for key in BASE_ENV_KEYS if key in cleaned}
    env.setdefault("TERM", "dumb")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["CODEX_HOME"] = str(codex_home)
    env[API_KEY_ENV] = api_key
    for name in environment_pass:
        if name in cleaned:
            env[name] = cleaned[name]
    return env, stripped, cloud


PROMPT_RULES = (
    "You are a local implementation worker. Work only in the current working directory.",
    "Create or modify files ONLY under the allowed paths listed below.",
    "Do not run git commit, git merge, git push, or git worktree commands.",
    "Do not read or print credentials, tokens, or environment secrets.",
    "The launcher, not you, owns the verification commands listed below: it runs that full "
    "suite itself after you return. Run only the focused checks the goal asks for, then "
    "return with the verification time reserved for the launcher.",
    "Implement the ordered contract with the assessment you were given plus targeted reads of "
    "the files it names; do not repeat whole-repository discovery that is already settled.",
    "If your context is compacted, resume the current task from your own git diff in this "
    "worktree instead of re-exploring from the beginning.",
    "Finish with a short summary of the change and the final test result.",
)

APPLE_PROMPT_NOTE = (
    "Native Apple verification: this job opted into --apple-simulator-udid {udid}. You run "
    "with full host permissions, so xcrun, simctl, xcodebuild and xcresulttool are available "
    "to your own tools directly against that one selected simulator: stay inside your task "
    "scope, do not boot, erase or touch any other device, and never wait on a device someone "
    "else leased. The launcher runs the verification commands listed above itself after you "
    "finish, and the native gate separately proves that build, install, launch and test "
    "actually work on the selected device."
)


def _attempt_budget_lines(deadline: Optional[Deadline], attempt_timeout: Optional[float],
                          reserve: Optional[float]) -> List[str]:
    """What the worker is actually allowed to spend on *this* attempt, in its own words.

    The total job budget alone is not actionable: the worker is killed much earlier, at
    the remaining budget after the verification reserve.
    """
    if attempt_timeout is None:
        return []
    effective = float(attempt_timeout)
    parts: List[str] = []
    if deadline is not None:
        left = max(0.0, deadline.remaining())
        if reserve is None:
            parts.append(f"remaining {left:.1f}s")
        else:
            parts.append(f"remaining {left:.1f}s - verification reserve {float(reserve):.1f}s")
    line = (f"- This attempt: the worker and its whole process group are killed at "
            f"{effective:.1f}s.")
    if parts:
        line += f" That cutoff is {', '.join(parts)}."
    return [line, "- Budget each step against that cutoff, not against the total above."]


def _budget_section(task: Task, deadline: Optional[Deadline],
                    attempt_timeout: Optional[float] = None,
                    reserve: Optional[float] = None) -> List[str]:
    """Total job budget and, when a live Deadline is handed in, what is actually left of it.

    The remainder is read from the shared job Deadline at prompt-construction time, so a retry
    sees what the earlier attempt and the verification pass consumed. A render without a
    Deadline says so instead of relabelling the total as a remainder.
    """
    lines = [
        "",
        "Task budget:",
        f"- Total for this whole task: {float(task.timeout_seconds):.0f}s, shared by setup, "
        "every model attempt and the launcher's independent verification. It never resets "
        "between attempts.",
        "- Budget the steps that are still ahead to fit what is left, and return early enough "
        "that the launcher can run the verification commands above itself.",
    ]
    if deadline is None:
        lines.append("- Remaining now: not measured by this render; the launcher tracks the "
                     "shared budget itself.")
        lines.extend(_attempt_budget_lines(deadline, attempt_timeout, reserve))
        return lines
    left = max(0.0, deadline.remaining())
    ends_at = (datetime.now(timezone.utc) + timedelta(seconds=left)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"- Remaining now: {left:.1f}s of that total (hard deadline {ends_at} UTC).")
    lines.extend(_attempt_budget_lines(deadline, attempt_timeout, reserve))
    return lines


def build_prompt(task: Task, worktree: Path, feedback: Optional[str] = None,
                 apple_udid: Optional[str] = None,
                 deadline: Optional[Deadline] = None,
                 attempt_timeout: Optional[float] = None,
                 reserve: Optional[float] = None) -> str:
    lines = [
        f"Task {task.task_id} (base commit {task.base_ref}).",
        *PROMPT_RULES,
        "",
        "Goal:",
        task.goal.strip(),
        "",
        "Acceptance criteria:",
        *[f"- {item}" for item in task.acceptance_criteria],
        "",
        "Allowed paths:",
        *[f"- {item}" for item in task.allowed_paths],
        "",
        "Launcher-owned verification commands (argv arrays, run from the worktree root by the "
        "launcher after you return):",
        *[f"- {json.dumps(argv)}" for argv in task.test_commands],
        *_budget_section(task, deadline, attempt_timeout=attempt_timeout, reserve=reserve),
        "",
        f"Worktree: {worktree}",
    ]
    if apple_udid:
        lines += ["", APPLE_PROMPT_NOTE.format(udid=apple_udid)]
    if feedback:
        lines += ["", "Previous attempt feedback (fix this):", feedback.strip()]
    return "\n".join(lines) + "\n"


def summarize_failure(attempt: Dict[str, Any], tests: Sequence[Dict[str, Any]],
                      violations: Optional[Sequence[Dict[str, str]]] = None) -> str:
    lines: List[str] = []
    if attempt.get("timed_out"):
        lines.append(f"- The previous attempt was killed after exceeding its deadline "
                     f"({attempt.get('elapsed_seconds')}s). Use fewer, faster steps.")
    elif attempt.get("returncode") not in (0, None):
        lines.append(f"- The previous attempt exited with code {attempt['returncode']}.")
        if attempt.get("stderr_excerpt"):
            lines.append(f"- Worker stderr tail: {attempt['stderr_excerpt']}")
        if attempt.get("model_routing") == "mismatch":
            lines.append(f"- Model routing evidence: {attempt.get('observed_models')}")
    for test in tests:
        if test.get("status") == "blocked":
            lines.append(f"- Verification could not run: {test.get('command')}")
        elif test.get("returncode") not in (0, None):
            lines.append(f"- Command {json.dumps(test.get('command'))} exited "
                         f"{test.get('returncode')}; output tail: {test.get('stdout_tail', '')}")
    for violation in (violations or [])[:10]:
        lines.append(f"- Out-of-scope change {violation.get('path')}: {violation.get('reason')}. "
                     "Revert it and keep edits inside allowed_paths.")
    if not lines:
        lines.append("- The previous attempt did not satisfy the acceptance criteria.")
    return "\n".join(lines)


def parse_worker_log(text: str) -> Dict[str, Any]:
    """Compatibility wrapper over the live event accumulator."""
    accumulator = observation.EventAccumulator()
    for line in text.splitlines():
        accumulator.add_raw_line(line)
    return accumulator.result()


def typed_worker_error_text(text: str) -> List[str]:
    """Extract failure detail only from documented terminal/error event shapes.

    Ordinary assistant messages are intentionally ignored: they are worker prose, not
    independent evidence for the planner's failure classification.
    """
    found = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") not in observation.TERMINAL_EVENT_TYPES:
            continue
        error = event.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("code")
        elif isinstance(error, str):
            value = error
        else:
            value = None
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    return found


def model_routing(evidence: Dict[str, Any], alias: str) -> str:
    observed = evidence.get("observed_models") or []
    if not observed:
        return "absent"
    if observed == [alias]:
        return "ok"
    return "mismatch"


# --------------------------------------------------------------------------------------
# Independent verification inside the permissions deny wrapper
# --------------------------------------------------------------------------------------
def verification_argv(sandbox: Dict[str, Any], codex_bin: str, worktree: Path,
                      argv: Sequence[str]) -> List[str]:
    """Return the command run inside the launcher's allow-default deny wrapper."""
    return list(argv)


def full_host_env(env: Dict[str, str]) -> Dict[str, str]:
    """Plain verification environment for the full-host backend: no confinement redirects.

    Nothing is rerouted into a job scratch directory: the deny wrapper (when applied) only takes
    away secrets, the primary checkout and the launcher state, so ``HOME``/``CODEX_HOME`` and the
    Foundation, temp and module-cache locations stay exactly as the job had them. Only ``IBToolNeverDeque=1`` is set -- forcibly, overriding any incoming
    value -- so actool/ibtool run in-process and cannot reuse an ``ibtoold`` that an earlier job
    started with different paths.
    """
    bounded = dict(env)
    # Forced, not defaulted: a hostile or stale incoming value must never let actool/ibtool
    # reuse an ibtoold that an earlier job started with different paths.
    bounded["IBToolNeverDeque"] = "1"
    return bounded


def probe_sandbox(codex_bin: str, worktree: Path, env: Dict[str, str],
                  secrets: Sequence[str] = (),
                  deadline: Optional[Deadline] = None) -> Dict[str, Any]:
    """Prove the plain full-host runner can execute before verification is trusted."""
    timeout = deadline.cap(PROBE_TIMEOUT_SECONDS) if deadline is not None else PROBE_TIMEOUT_SECONDS
    if timeout < MIN_ATTEMPT_BUDGET_SECONDS:
        return {"available": False, "profile": FULL_HOST_PROFILE, "network": "enabled",
                "backend": FULL_HOST_BACKEND, "extra_writable_roots": [],
                "confinement": FULL_HOST_CONFINEMENT,
                "detail": f"the total job budget has only {timeout:.1f}s left, so the full-host "
                          "readiness command was not run and no verification command will be",
                "argv_template": list(FULL_HOST_PROBE_ARGV)}
    try:
        result = run_bounded(list(FULL_HOST_PROBE_ARGV), cwd=worktree, env=env, timeout=timeout)
    except OSError as error:
        return {"available": False, "profile": FULL_HOST_PROFILE, "network": "enabled",
                "backend": FULL_HOST_BACKEND, "extra_writable_roots": [],
                "confinement": FULL_HOST_CONFINEMENT,
                "detail": f"cannot run the trivial full-host command: {error}",
                "argv_template": list(FULL_HOST_PROBE_ARGV)}
    stdout = result["stdout"].decode("utf-8", "replace")
    available = result["returncode"] == 0 and PROBE_MARKER in stdout and not result["timed_out"]
    detail = (stdout + result["stderr"].decode("utf-8", "replace")).strip()
    if result["timed_out"]:
        detail = f"full-host readiness command timed out after {timeout:.1f}s: {detail}"
    return {"available": available, "profile": FULL_HOST_PROFILE, "network": "enabled",
            "backend": FULL_HOST_BACKEND, "extra_writable_roots": [],
            "confinement": FULL_HOST_CONFINEMENT,
            "detail": redact(detail[-500:], secrets) or ("probe ok" if available else "probe failed"),
            "probe_returncode": result["returncode"], "probe_timed_out": result["timed_out"],
            "argv_template": list(FULL_HOST_PROBE_ARGV)}


# --------------------------------------------------------------------------------------
# The deny wrapper
# --------------------------------------------------------------------------------------
@dataclass
class DenyWrapper:
    """The job's Seatbelt deny wrapper, and the report's ``permissions`` record it keeps current.

    ``profile`` is set only once the profile is proven to apply; until then ``wrap`` returns
    the command unchanged and ``record["reason"]`` says why. ``collect`` adds the denials a
    command's output shows to ``record["denials"]``.
    """

    perms: permissions.Permissions
    primary_repo: Path
    state_dir: Path
    record: Dict[str, Any]
    worktree: Optional[Path] = None
    profile: Optional[Path] = None
    # The primary's git directories (common dir, own admin dir): a denial naming one is a write to
    # the primary checkout even when the checkout keeps its git dir elsewhere (R22).
    git_dirs: Tuple[Path, ...] = ()

    @property
    def applied(self) -> bool:
        return self.profile is not None

    def wrap(self, argv: Sequence[str]) -> List[str]:
        if self.profile is None:
            return [str(item) for item in argv]
        return permissions.wrap_argv(argv, self.profile)

    def collect(self, text: str, source: str) -> None:
        """Record the wrapper denials ``text`` shows, each path and kind once (first source).

        ``worktree`` is passed so a refused write whose message names only a destination inside
        the job's own worktree (a refused hard link or rename *into* it) is reported with cause
        ``unknown`` instead of being missed.
        """
        if not text:
            return
        seen = {(item["path"], item["kind"]) for item in self.record["denials"]}
        for found in permissions.find_denials(text, self.perms, self.primary_repo,
                                              state_dir=self.state_dir, worktree=self.worktree,
                                              git_dirs=self.git_dirs):
            if (found["path"], found["kind"]) not in seen:
                seen.add((found["path"], found["kind"]))
                self.record["denials"].append(dict(found, source=source))


def deny_list_applied(report: Dict[str, Any]) -> bool:
    """Whether the job's deny wrapper was applied (``False`` for reports that predate it)."""
    return bool((report.get("permissions") or {}).get("deny_list_applied"))


def load_job_permissions(options: "JobOptions", report_dir: Path) -> permissions.Permissions:
    """The user's permissions list for this job, or a ``preflight_failed`` refusal.

    A list that cannot be read or parsed is never replaced by the defaults (the job would run
    without the denials the user wrote), and a list that would hide the repository, the worktree
    root, the state directory or the Codex executable, or whose ``deny_write`` entry would stop
    the job writing its state, report directory, worktree root or Codex home, refuses the job
    naming the conflict.
    """
    try:
        perms = permissions.load_permissions(options.state_dir)
    except permissions.PermissionsError as error:
        raise PreflightError(f"the permissions list cannot be used: {error}") from None
    codex = shutil.which(options.codex_bin) or options.codex_bin
    problems = permissions.check_conflicts(
        perms, repo=options.task.repo_path, worktree_root=options.state_dir / "worktrees",
        state_dir=options.state_dir, codex_bin=Path(codex), report_dir=report_dir,
        codex_home=report_dir / CODEX_HOME_NAME)
    if problems:
        raise PreflightError("the permissions list conflicts with this job: " + "; ".join(problems))
    return perms


def last_message_path(report_dir: Path, attempt_number: int) -> Path:
    """The ``-o`` file Codex writes the worker's final message to for one attempt."""
    return report_dir / f"attempt-{attempt_number}.last-message.txt"


def _covers_path(parent: Path, child: Path) -> bool:
    """``child`` is ``parent`` itself or lives under it, comparing real paths."""
    parent_real = os.path.realpath(str(parent))
    child_real = os.path.realpath(str(child))
    return child_real == parent_real or child_real.startswith(parent_real + os.sep)


def job_write_roots(report_dir: Path, worktree: Path, codex_home: Path) -> List[Path]:
    """The directory roots a wrapped process may write: the ones that are rename-protected so the
    launcher never follows one that was swapped for a symlink."""
    return [worktree, codex_home, report_dir / NATIVE_SCRATCH_NAME, report_dir / SCREENSHOTS_DIR_NAME]


def job_writable_paths(report_dir: Path, worktree: Path, codex_home: Path, attempts: int,
                       env: Dict[str, str]) -> List[Path]:
    """What a wrapped process of this job may write inside the launcher's state directory.

    Its worktree, its Codex home, the ``-o`` file of each attempt, the native scratch and the
    screenshots directory. ``TMPDIR`` only matters here when it sits inside the state directory
    (otherwise ``allow default`` already covers it); even then it is re-opened only when it does
    not cover the report directory, so a ``TMPDIR`` that is, or contains, the report directory is
    never re-opened and cannot re-open the reports, logs, patch or this job's own profile
    wholesale. Nothing else of the report directory is re-opened: that evidence is the launcher's,
    and a worker able to rewrite ``permissions.sb`` would choose the profile of every later
    attempt and check.
    """
    paths = job_write_roots(report_dir, worktree, codex_home)
    paths += [last_message_path(report_dir, number) for number in range(1, attempts + 1)]
    tmpdir = env.get("TMPDIR")
    if tmpdir and not _covers_path(Path(tmpdir), report_dir):
        paths.append(Path(tmpdir))
    return paths


def worktree_git_layout(worktree: Path) -> Dict[str, str]:
    """The job worktree's real git dir and common dir, read now (before the worker runs, so the
    gitfile and ``commondir`` are still trustworthy).

    ``GIT_DIR`` is the linked worktree's own admin dir under ``<primary>/.git/worktrees/<name>``;
    ``GIT_COMMON_DIR`` is the primary ``.git``. Pinning these on every later git command makes it
    ignore a gitfile or ``commondir`` the worker may have repointed.
    """
    git_dir = git(worktree, "rev-parse", "--absolute-git-dir").stdout.decode("utf-8", "replace").strip()
    common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_dir = common.stdout.decode("utf-8", "replace").strip()
    return {"GIT_DIR": git_dir, "GIT_COMMON_DIR": common_dir, "GIT_WORK_TREE": str(worktree)}


def git_admin_fingerprint(layout: Dict[str, str], worktree: Path) -> Dict[str, str]:
    """A fingerprint of everything that redirects git for this worktree: the worktree's ``.git``
    gitfile and the admin dir's ``commondir``/``gitdir``/``config.worktree``. Compared before and
    after the worker; any difference is an audit violation, never informational."""
    admin = Path(layout["GIT_DIR"])
    targets = {"gitfile": Path(worktree) / ".git"}
    # The redirect files are named once, in permissions: the same list the profile keeps denied.
    for name in permissions.GIT_ADMIN_REDIRECTS:
        targets[name] = admin / name
    return {name: path_state(path) for name, path in targets.items()}


def git_redirect_changes(before: Dict[str, str], layout: Dict[str, str], worktree: Path) -> List[str]:
    """The redirect files whose state differs from ``before`` (the fingerprint taken before the
    worker ran); empty while every one is as the launcher left it."""
    try:
        after = git_admin_fingerprint(layout, Path(worktree))
    except OSError:
        after = {}
    return sorted(name for name in before if before.get(name) != after.get(name))


def git_redirect_violation(changed: Sequence[str], git_skipped: bool = False) -> Dict[str, str]:
    """The audit violation for a changed git redirect file (``git_skipped``: the audit then did
    not run git at all, so nothing the redirect could steer ever ran)."""
    reason = ("worker changed git redirect file(s) " + ", ".join(changed)
              + " (gitfile/commondir/gitdir/config.worktree)")
    if git_skipped:
        reason += "; git was not run on this worktree again, so nothing it could steer ran"
    else:
        reason += "; the launcher pinned its own git layout so the audit was not steered"
    return {"path": ".git", "reason": reason + ", but this is refused, not informational"}


def note_git_admin_tamper(report: Dict[str, Any], worktree: Path) -> List[str]:
    """Append an audit violation when the worker changed a git redirect file for this worktree.

    Compares the live fingerprint with the one captured before the worker ran. A change means the
    worker tried to repoint the launcher's own (unwrapped) git at a repository it controls. The
    launcher already pinned ``GIT_DIR``/``GIT_COMMON_DIR``/``GIT_WORK_TREE`` and the audit skips
    git once a redirect file differs, so the attempt did not steer anything, but a repointed admin
    file is still a refusal, never informational, so the change itself is recorded as a violation.
    Returns the names that changed.
    """
    before = report.get("_git_admin_before")
    layout = report.get("_git_layout")
    if not before or not layout:
        return []
    changed = git_redirect_changes(before, layout, Path(worktree))
    if not changed:
        return []
    report.setdefault("git_admin_tampered", changed)
    # Ensure the violation is present even after a later audit reset report["violations"]; it is
    # keyed on path ".git" and reason prefix so it is not appended twice (the audit records the
    # same violation itself when it skipped git).
    violations = report.setdefault("violations", [])
    if not any(v.get("path") == ".git" and "git redirect file" in v.get("reason", "")
               for v in violations):
        violations.append(git_redirect_violation(changed))
    return changed


def safe_read_job_text(path: Path, report_dir: Path, secrets: Sequence[str]
                       ) -> Tuple[str, Optional[str], Optional[str]]:
    """Read a launcher-read job file without following a link or waiting on a FIFO.

    ``open_regular_file`` refuses a symlink, a FIFO or any non-regular file, and the real path
    must stay inside ``report_dir``. Returns ``(text, missing, tampered)``: ``text`` with both
    ``None`` when it was read; ``("", reason, None)`` when the file is simply not there (a worker
    that crashed or timed out before writing it is not tampering); ``("", None, reason)`` when it
    is there but something was swapped in or in the way (a symlink, a non-regular file, a path
    outside the report directory, or an error reading it), so evidence is never read *through* a
    swap.
    """
    try:
        with open_regular_file(path) as handle:
            if not _covers_path(report_dir, path):
                return "", None, "resolves outside the report directory, so it was not read"
            data = handle.read()
    except NotRegularFile:
        return "", None, "not a regular file, so it was not read"
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            return "", None, "a symlink is in place of the file, so it was not read"
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return "", f"could not be opened ({error.strerror})", None
        return "", None, f"could not be read ({error.strerror})"
    return redact(data.decode("utf-8", "replace"), secrets), None, None


def setup_deny_wrapper(options: "JobOptions", report: Dict[str, Any],
                       perms: permissions.Permissions, report_dir: Path, worktree: Path,
                       codex_home: Path, git_layout: Dict[str, str], env: Dict[str, str],
                       secrets: Sequence[str], deadline: Deadline) -> DenyWrapper:
    """Write ``<report_dir>/permissions.sb`` and decide whether this job runs under it.

    The profile is applied only after ``sandbox-exec`` has run a trivial command under it.
    Without ``sandbox-exec``, or when it exits 71 because the profile cannot be applied here (the
    launcher is itself inside a sandbox), the job runs without the wrapper and the report says
    so with the reason (spec §6.14: never silent). Any other probe failure means the generated
    profile does not work, and nothing is run under a profile that does not work: the job is
    blocked.
    """
    record = report["permissions"]
    # Codex needs metadata on CODEX_HOME itself during startup; all child content stays hidden
    # except for auth.json and the observed temporary helper subtree.
    cloud_home = Path(report["cloud_auth"]["source_home"]) if report.get("cloud_auth") else None
    helper_scratch = cloud_home / "tmp" / "arg0-macos" if cloud_home else None
    profile_perms = (replace(perms, deny_read=list(perms.deny_read) + [str(cloud_home)])
                     if cloud_home else perms)
    try:
        # The job worktree's own GIT_COMMON_DIR (read before any worker ran) is the primary's
        # real common dir whatever shape the primary is; the profile also resolves the primary's
        # own git dir (a linked-worktree primary has its admin dir apart from it).
        text = permissions.seatbelt_profile(
            profile_perms, primary_repo=options.task.repo_path, state_dir=options.state_dir,
            allowed_writes=job_writable_paths(report_dir, worktree, codex_home,
                                              options.task.max_attempts, env),
            protect_write_roots=job_write_roots(report_dir, worktree, codex_home),
            job_git_admin=git_layout["GIT_DIR"], job_worktree=worktree,
            git_common_dir=git_layout["GIT_COMMON_DIR"],
            additional_deny_read=[cloud_home] if cloud_home else (),
            allow_read_metadata=[cloud_home] if cloud_home else (),
            allow_read=[cloud_home / "auth.json"] if cloud_home else (),
            allow_read_subpaths=[helper_scratch] if helper_scratch else (),
            allow_write=[cloud_home / "auth.json", cloud_home / "tmp",
                         cloud_home / "tmp" / "arg0-macos"] if cloud_home else (),
            allow_write_subpaths=[helper_scratch] if helper_scratch else ())
        primary_git_dirs = permissions.resolve_git_dirs(options.task.repo_path)
    except permissions.PermissionsError as error:
        raise PreflightError(f"the permissions deny profile cannot be built: {error}") from None
    profile = report_dir / PERMISSIONS_PROFILE_NAME
    profile.write_text(text, encoding="utf-8")
    record.update(mode=perms.mode, profile=str(profile), deny_list_applied=False,
                  profile_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    wrapper = DenyWrapper(profile_perms, options.task.repo_path, options.state_dir, record, worktree=worktree,
                          git_dirs=(Path(git_layout["GIT_COMMON_DIR"]),
                                    *(Path(path) for path in primary_git_dirs)))
    if not permissions.wrapper_available():
        record["reason"] = (f"{permissions.SANDBOX_EXEC} is not available on this host, so the "
                            "deny list was not applied: the job ran with full host access")
        return wrapper
    timeout = deadline.cap(PROBE_TIMEOUT_SECONDS)
    if timeout < MIN_ATTEMPT_BUDGET_SECONDS:
        raise DeadlineExpired(f"only {timeout:.1f}s of the job budget is left, so the deny "
                              "wrapper could not be probed and nothing was run")
    try:
        result = run_bounded(permissions.wrap_argv(WRAPPER_PROBE_ARGV, profile), cwd=worktree,
                             env=env, timeout=timeout)
    except OSError as error:
        raise BlockedError(f"the deny wrapper {permissions.SANDBOX_EXEC} could not be started: "
                           f"{error}") from error
    detail = redact(_tail((result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace"),
                          500), secrets)
    if result["returncode"] == 0 and not result["timed_out"]:
        wrapper.profile = profile
        record.update(deny_list_applied=True, reason=None)
        return wrapper
    if result["returncode"] == WRAPPER_NOT_APPLIED_EXIT and not result["timed_out"]:
        record["reason"] = (f"{permissions.SANDBOX_EXEC} exited {WRAPPER_NOT_APPLIED_EXIT}: it "
                            "could not apply the deny profile here (the launcher is probably "
                            "itself inside a sandbox), so the deny list was not applied and the "
                            f"job ran with full host access: {detail or 'no output'}")
        return wrapper
    state = "timed out" if result["timed_out"] else f"exited {result['returncode']}"
    raise BlockedError(f"the deny wrapper probe {state} under {profile}: {detail or 'no output'}; "
                       "no worker or check is run under a profile that does not work")


def worker_command_outputs(text: str) -> List[str]:
    """The output of every shell command the worker ran, from its JSONL stream.

    Only completed ``command_execution`` items are read: that is where a command the wrapper
    refused says so, and command output echoing ``Operation not permitted`` is trusted. Model
    messages and reasoning are never read, so a model quoting a denial *in messages* is not taken
    for one.
    """
    outputs: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "command_execution"):
            output = item.get("aggregated_output")
            if isinstance(output, str) and output:
                outputs.append(output)
    return outputs


def run_verification(task: Task, worktree: Path, env: Dict[str, str], codex_bin: str,
                     sandbox: Dict[str, Any], secrets: Sequence[str],
                     deadline: Deadline, wrapper: Optional[DenyWrapper] = None
                     ) -> List[Dict[str, Any]]:
    """Run each contract command, or report it blocked, exactly as the backend runs it.

    There is no per-test timeout reset: every command is capped by whatever is left of the
    single total job budget, so ``timeout_seconds`` bounds preflight, probe, attempts and all
    verification together.

    The backend comes from the probe result. The default full-host backend runs the plain
    command with the launcher's own permissions and network access and records
    ``sandboxed: false``; the legacy Codex workspace-write sandbox and the explicit Apple
    Seatbelt backend confine the command when a job selected them. Whatever the backend, the
    command runs behind the job's deny ``wrapper`` when one is applied (an inner Seatbelt
    profile still applies inside it: nested profiles intersect). Each entry reports which
    backend ran it, whether anything confined it and ``deny_list_applied``.
    """
    results: List[Dict[str, Any]] = []
    backend = FULL_HOST_BACKEND
    full_host = True
    denied = wrapper is not None and wrapper.applied
    for index, argv in enumerate(task.test_commands, start=1):
        left = deadline.remaining()
        if left < MIN_ATTEMPT_BUDGET_SECONDS:
            results.append({
                "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                "backend": backend,
                "reason": f"verification not run: the {deadline.total_seconds:.0f}s total job "
                          f"budget has only {left:.1f}s left",
            })
            continue
        if full_host:
            if not sandbox.get("available"):
                detail = str(sandbox.get("detail")
                             or "the full-host readiness command did not run")
                results.append({
                    "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                    "backend": backend, "confinement": FULL_HOST_CONFINEMENT,
                    "reason": f"full-host execution is unavailable: {detail}",
                })
                continue
        timeout = deadline.cap(float(task.timeout_seconds))
        try:
            wrapped = verification_argv(sandbox, codex_bin, worktree, argv)
            if wrapper is not None:
                wrapped = wrapper.wrap(wrapped)
            command_env = full_host_env(env)
        except BlockedError as error:
            results.append({
                "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                "backend": backend, "reason": redact(str(error), secrets),
            })
            continue
        result = run_bounded(wrapped, cwd=worktree, env=command_env, timeout=timeout)
        output = redact((result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace"), secrets)
        over_limit = bool(result.get("output_limit_exceeded"))
        passed = result["returncode"] == 0 and not result["timed_out"] and not over_limit
        entry: Dict[str, Any] = {
            "command": argv, "sandboxed": not full_host, "log": str(index),
            "backend": backend, "deny_list_applied": denied,
            "confinement": ((FULL_HOST_DENY_CONFINEMENT if denied else FULL_HOST_CONFINEMENT)
                            if full_host else str(sandbox.get("apple_argv_prefix") or backend)),
            "status": "passed" if passed else "failed",
            "returncode": result["returncode"], "timed_out": result["timed_out"],
            "output_limit_exceeded": over_limit,
            "elapsed_seconds": result["elapsed_seconds"], "stdout_tail": output[-1000:],
            "output_bytes": len(output.encode("utf-8")), "output_text": output,
            "termination": result["termination"],
        }
        if result["timed_out"]:
            entry["reason"] = (f"verification command timed out after {timeout:.1f}s of the "
                               "shared job budget")
        elif over_limit:
            entry["reason"] = (
                f"verification command output exceeded the {observation.CAPTURE_LIMIT_BYTES} "
                "byte capture limit, so the retained output is incomplete and cannot show "
                "that the command passed; see the separate termination evidence for what "
                "happened to the process group")
        elif not passed:
            entry["reason"] = (f"verification command exited {result['returncode']} with "
                               "full-host execution (sandboxed=false)" if full_host else
                               f"verification command exited {result['returncode']} in the sandbox")
        results.append(entry)
    return results


# --------------------------------------------------------------------------------------
# Job orchestration
# --------------------------------------------------------------------------------------
@dataclass
class JobOptions:
    task: Task
    state_dir: Path
    codex_bin: str
    lock_timeout: float = LOCK_TIMEOUT_SECONDS
    settings_path: Path = OMLX_SETTINGS
    # The loaded model to use (id or alias); ``None`` lets discovery choose the only one.
    requested_model: Optional[str] = None
    # An optional *lower* cap on the context window discovery reports; never raises it.
    context_cap: Optional[int] = None
    # ``--load``: ask oMLX to load ``requested_model`` before discovery.
    load_model: bool = False
    omlx_fetch: omlx_discovery.HttpFetch = omlx_discovery.http_fetch
    verify: bool = True
    apple_simulator_udid: Optional[str] = None
    maximum: int = 1
    native_preflight_plan: Optional[Path] = None
    preflight_only: bool = False
    workstream: Optional[str] = None
    job_kind: str = "implementation"
    tool_config: Optional[Path] = None
    escalates: Optional[str] = None
    # Runs the two informational Codex calls of the compatibility check (``subprocess.run``
    # signature); tests replace it so no installed Codex is needed.
    codex_run: Callable[..., Any] = subprocess.run


def new_report(options: JobOptions) -> Dict[str, Any]:
    task = options.task
    return {
        "schema_version": 1,
        "launcher_pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_id": f"{_now_stamp()}-{task.task_id}-{uuid.uuid4().hex[:6]}",
        "task_id": task.task_id,
        "kind": task.kind,
        "status": "worker_failed",
        "tier": "cloud" if task.tier.kind == "cloud" else "local",
        "escalates": options.escalates,
        "failure_reason": None,
        # Filled from live discovery once preflight has succeeded.
        "endpoint": None,
        "api_model": None,
        "model_profile": {},
        "context_window": None,
        "model_fallback": False,
        "provider": PROVIDER_ID,
        # The installed Codex CLI's compatibility evidence, the job's own Codex home and the
        # sha256 of the model catalog generated in it (pinned by the launch self-check).
        "codex_compat": None,
        "codex_home": None,
        "catalog_sha256": None,
        "sandbox_mode": "read-only" if task.kind == "read" else "danger-full-access",
        "repo_path": str(task.repo_path),
        "base_ref": task.base_ref,
        "base_commit": None,
        "head_commit": None,
        "sandbox_profile": FULL_HOST_PROFILE,
        "worktree": None,
        "worktree_preserved": False,
        "lease": {"mode": "exclusive", "capacity": 1, "slot": None,
                  "legacy_lock": "exclusive", "resource": None, "wait_seconds": 0.0,
                  "waits": {}, "pool": str(options.state_dir), "task_id": task.task_id,
                  "repo_identity": None},
        "report_dir": None,
        "dirty_primary_files": [],
        "primary_unchanged": True,
        # With the deny wrapper applied the worker cannot write the primary checkout, so a change
        # seen there was made by the parent or the user: reported, not attributed, not a failure.
        "primary_changed_during_job": False,
        "primary_fingerprint_before": None,
        "primary_fingerprint_after": None,
        # The job's Seatbelt deny wrapper (spec §6.2, §6.14), filled once it is set up: the
        # profile written to ``<report_dir>/permissions.sb`` and its hash, whether it was applied
        # (and why not), and the denials the worker's commands and the checks ran into.
        "permissions": {"mode": None, "deny_list_applied": False, "profile": None,
                        "profile_sha256": None,
                        "reason": "not set up: the job stopped before anything ran under it",
                        "denials": []},
        "budget": {"total_seconds": float(task.timeout_seconds), "elapsed_seconds": 0.0,
                   "remaining_seconds": float(task.timeout_seconds), "exceeded": False},
        "workstream": options.workstream or task.workstream,
        "job_kind": options.job_kind,
        "native_preflight": None,
        "preflight": {},
        "sandbox": {},
        "attempts": [],
        "auto_retries": [],
        "screenshots": [],
        "tests": [],
        "changed_paths": [],
        "violations": [],
        "ignored_paths": [],
        "patch_path": None,
        "patch_bytes": 0,
        "patch_excerpt": None,
        "diffstat": "",
        # Set from the worker's own scoped progress before verification can write anything:
        # a patch that only verification produced proves nothing about the implementation.
        "worker_patch_present": False,
        "token_counts": {"input_tokens": None, "output_tokens": None},
        "elapsed_seconds": 0.0,
        "logs": [],
        "limitations": list(LIMITATIONS),
    }


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.realpath(str(child))).relative_to(os.path.realpath(str(parent)))
        return True
    except ValueError:
        return False


def ensure_report_dir(options: JobOptions, report: Dict[str, Any]) -> Optional[Path]:
    """Create the report directory *outside* the repository so failures stay persisted."""
    if report.get("report_dir"):
        return Path(str(report["report_dir"]))
    if _inside(options.state_dir, options.task.repo_path):
        return None
    try:
        report_dir = options.state_dir / "reports" / report["job_id"]
        report_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    report["report_dir"] = str(report_dir)
    return report_dir


def _policy_check(argv: Sequence[str], profile: omlx_discovery.ServerProfile,
                  context_window: int, catalog: Optional[Tuple[Path, str]] = None,
                  sandbox_mode: str = REQUIRED_SANDBOX_MODE,
                  output_schema: Optional[Path] = None) -> None:
    """Refuse to launch a worker whose pinned local policy drifted in any way."""
    problems = missing_config_pins(argv, profile, context_window, catalog,
                                   sandbox_mode=sandbox_mode, output_schema=output_schema)
    if problems:
        raise BlockedError("worker policy self-check refused the launch: " + "; ".join(problems))


def _apple_opt_in(options: JobOptions, report: Dict[str, Any], report_dir: Path,
                  worktree: Path, sandbox: Dict[str, Any], env: Dict[str, str],
                  secrets: Sequence[str], deadline: Deadline) -> Dict[str, Any]:
    """Record the native scope on the full-host backend; no confinement probe runs here.

    A native job no longer swaps in a confined backend: the worker and the verification
    commands already run with the launcher's full host permissions, so no Seatbelt profile is
    generated, no profile readiness probe is run, and no scratch confinement is set up. What
    the opt-in still pins is *scope*, not confinement: exactly one selected simulator UUID
    (held by the pool lease) and the native toolchain environment, with ``HOME``/``CODEX_HOME``
    untouched. Whether the selected device can really be built for, installed, launched and
    tested is proven positively by the native gate, which runs those commands for real.
    """
    updated = dict(sandbox)
    updated.update({
        "backend": FULL_HOST_BACKEND,
        "profile": FULL_HOST_PROFILE,
        "network": "enabled",
        "confinement": FULL_HOST_CONFINEMENT,
        "apple_simulator_udid": options.apple_simulator_udid,
        "native_backend": FULL_HOST_BACKEND,
        "native_env": {"IBToolNeverDeque": "1"},
        "native_probe": "not run (full-host backend: the native gate is the positive check)",
        "extra_writable_roots": [],
    })
    if not options.verify:
        updated["apple"] = {"skipped": "--no-verify", "probe": "not run"}
    else:
        updated["apple"] = {
            "probe": "not run",
            "backend": FULL_HOST_BACKEND,
            "simulator_udid": str(options.apple_simulator_udid),
            "sandbox_exec": None,
            "profile": None,
            "env": dict(updated["native_env"]),
            "env_preserved": ["HOME", "CODEX_HOME"],
        }
    report["sandbox"] = updated
    return updated


def _read_prompt(task: Task, worktree: Path) -> str:
    return "\n".join([
        f"Read task {task.task_id} (base commit {task.base_ref}).",
        "Answer the question using only the current worktree. Do not modify any files.",
        "Return the answer as JSON matching the supplied output schema, with citations that "
        "quote the cited source lines exactly apart from whitespace normalization.",
        "Question:", task.question or "",
        "Scope paths:", *["- " + path for path in task.scope_paths],
        "Worktree:", str(worktree),
    ]) + "\n"


def _execute_read_job(options: JobOptions, report: Dict[str, Any], deadline: Deadline,
                      profile: omlx_discovery.ServerProfile, context_window: int,
                      worktree: Path, report_dir: Path, env: Dict[str, str],
                      loaded: Dict[str, Any], catalog_path: Path,
                      catalog: Optional[Tuple[Path, str]], wrapper: DenyWrapper,
                      secrets: Sequence[str], stream: Any) -> None:
    """Run one structured, read-only turn and release only schema/citation checked data."""
    task = options.task
    schema_path = report_dir / "answer.schema.json"
    schema = read_answers.envelope_schema(task.answer_schema or {})
    schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    if (task.tier.kind != "cloud" and
            recheck_pinned_model(profile, options.settings_path, options.omlx_fetch, 1) is not None):
        report["status"] = "answer_invalid"
        report["failure_reason"] = "the pinned model changed before the read attempt"
        return
    remaining = deadline.require("read attempt")
    stall = stall_limit(profile, context_window)
    last_path = last_message_path(report_dir, 1)
    prompt_path = report_dir / "attempt-1.prompt.txt"
    stderr_path = report_dir / "worker-attempt-1.stderr.log"
    jsonl_path = report_dir / "worker-attempt-1.jsonl"
    prompt = _read_prompt(task, worktree)
    prompt_path.write_text(redact(prompt, secrets), encoding="utf-8")
    argv = worker_argv_for_tier(options, worktree, profile, context_window, last_path,
                                loaded["overrides"], list(loaded["environment"]), catalog_path,
                                sandbox_mode="read-only", output_schema=schema_path)
    if task.tier.kind == "cloud":
        problems = cloud_policy_check(argv, task.tier.model or "", task.tier.reasoning_effort,
                                      sandbox_mode="read-only")
        if problems:
            raise BlockedError("cloud worker policy self-check refused the launch: " + "; ".join(problems))
    else:
        _policy_check(argv, profile, context_window, catalog,
                      sandbox_mode="read-only", output_schema=schema_path)
    if stream is not None:
        stream.start_attempt(report["job_id"])
    report["_active_attempt"] = {"before_patch": ""}
    observer = observation.RunObserver(
        Path(report["report_dir"]), secrets=secrets, attempt=1, job_id=report["job_id"],
        active_dir=options.state_dir / "active", worker_command=options.codex_bin,
        job={"started_at": report.get("started_at"), "workstream": report.get("workstream"),
             "job_kind": report.get("job_kind")})
    token = _RUN_OBSERVER.set(observer)
    timeout = deadline.cap(remaining)
    try:
        result = run_bounded(
            wrapper.wrap(argv), cwd=worktree, env=env, timeout=timeout,
            stdin_data=prompt.encode("utf-8"), stall_seconds=stall["seconds"],
            on_stall=(None if task.tier.kind == "cloud" else
                lambda: server_busy(options.settings_path, options.omlx_fetch,
                                    min(deadline.end, time.monotonic() + timeout))))
    except BaseException:
        observer.finish(status="failed")
        snapshot = observer.snapshot()
        report["attempts"].append({"attempt": 1, "returncode": None, "timed_out": False,
                                   "interrupted": True,
                                   "elapsed_seconds": snapshot["usage"]["elapsed_seconds"],
                                   "usage": snapshot["usage"]})
        raise
    finally:
        _RUN_OBSERVER.reset(token)

    stdout_text = redact(result["stdout"].decode("utf-8", "replace"), secrets)
    stderr_text = redact(result["stderr"].decode("utf-8", "replace"), secrets)
    if observer.activity_count == 0:
        if result.get("stdout"):
            observer("stdout", result["stdout"])
        if result.get("stderr"):
            observer("stderr", result["stderr"])
    observer.finish(result=result)
    evidence = parse_worker_log(stdout_text)
    report["tooling"] = tools.mark_observations(report.get("tooling"), stdout_text)
    routing = model_routing(evidence, report["api_model"])
    usage = observer.snapshot()["usage"]
    for key in ("started_at", "finished_at", "elapsed_seconds"):
        evidence["usage"][key] = usage[key]
    attempt = {"attempt": 1, "returncode": result["returncode"],
               "timed_out": result["timed_out"], "stalled": bool(result.get("stalled")),
               "elapsed_seconds": result["elapsed_seconds"],
               "verification_reserve_seconds": 0.0,
               "worker_timeout_seconds": timeout, "limits": {"stall": stall,
                                                                  "budget_seconds": timeout},
               "has_scoped_patch": False, "usage": evidence["usage"],
               "termination": result["termination"],
               "observed_models": evidence["observed_models"], "model_routing": routing,
               "tool_call_events": evidence["tool_call_events"],
               "tool_started_count": evidence["tool_started_count"],
               "tool_completed_count": evidence["tool_completed_count"],
               "thread_id": evidence["thread_id"],
               "unsupported_tools": evidence["unsupported_tools"],
               "terminal_failures": evidence["terminal_failures"],
               "error_text": typed_worker_error_text(stdout_text),
               "token_counts": evidence["token_counts"],
               "stderr_excerpt": _tail(stderr_text),
               "last_message_excerpt": "", "logs": [str(prompt_path), str(jsonl_path),
                                                        str(stderr_path), str(last_path)]}
    report["attempts"].append(attempt)
    report["worker_patch_present"] = False
    report["tests"] = []
    report["logs"].extend(attempt["logs"])
    (report_dir / "worker-attempt-1.stderr.log").write_text(stderr_text, encoding="utf-8")
    (report_dir / "worker-attempt-1.jsonl").write_text(stdout_text, encoding="utf-8")

    message, missing, tampered = safe_read_job_text(last_path, report_dir, secrets)
    if missing:
        attempt["last_message_missing"] = missing
    if tampered:
        attempt["last_message_tampered"] = tampered
    # Read payloads are untrusted until the schema and every citation pass. Keep raw output out
    # of the launcher report so an invalid answer is never presented to the planner as evidence.
    attempt["last_message_excerpt"] = ""
    try:
        if missing or tampered:
            report["answer_bytes"] = len(message.encode("utf-8"))
        else:
            report["answer_bytes"] = last_path.stat().st_size
    except OSError:
        report["answer_bytes"] = len(message.encode("utf-8"))
    report["citations"] = []
    report["citation_verification"] = {"total": 0, "verified": 0, "failures": []}
    errors: List[str] = []
    if report["answer_bytes"] > task.max_answer_bytes:
        errors.append("answer exceeds max_answer_bytes (%d > %d)" %
                      (report["answer_bytes"], task.max_answer_bytes))
    elif missing or tampered:
        errors.append("the worker did not produce a readable answer envelope")
    else:
        try:
            envelope = json.loads(message)
        except (ValueError, TypeError):
            envelope = None
            errors.append("the worker output is not valid JSON")
        if envelope is not None:
            schema_errors = read_answers.validate(envelope, schema)
            errors.extend(schema_errors)
            citations = envelope.get("citations") if isinstance(envelope, dict) else None
            if isinstance(citations, list):
                checked = read_answers.verify_citations(worktree, citations)
                report["citation_verification"] = checked
                if checked["failures"]:
                    errors.extend("citation %d: %s" % (item["index"], item["reason"])
                                  for item in checked["failures"])
            if not errors and isinstance(envelope, dict):
                report["answer"] = envelope["answer"]
                report["citations"] = envelope["citations"]
    if result["returncode"] != 0 or result["timed_out"] or result.get("output_limit_exceeded"):
        errors.append("the read worker did not finish successfully")
    if routing == "mismatch":
        errors.append("worker model routing did not match the pinned model")
    report["status"] = "answer_invalid" if errors else "ok"
    if errors:
        report.pop("answer", None)
        report["citations"] = []
        report["failure_reason"] = "; ".join(errors[:10])
    report.pop("_active_attempt", None)


def progress(report: Dict[str, Any], phase: str) -> None:
    directory = report.get("report_dir")
    if not directory:
        return
    path = Path(directory) / "progress.json"
    previous = {}
    if path.is_file():
        with contextlib.suppress(OSError, ValueError):
            previous = json.loads(path.read_text())
    if phase == report.get("status"):
        previous.update(usage=report.get("usage"), timing=report.get("timing"),
                        elapsed_seconds=report.get("elapsed_seconds"),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        failure_reason=report.get("failure_reason"),
                        active=False)
    # The observer rewrites this file per attempt with its own attempt start; the job start
    # stays authoritative, so a live snapshot still reports job-level elapsed time.
    previous["started_at"] = report.get("started_at") or previous.get("started_at")
    previous.setdefault("last_event_at", None)
    previous.setdefault("job_started_at", previous["started_at"])
    previous.update(launcher_pid=report["launcher_pid"],
                    active=phase != report.get("status"),
                    job_id=report["job_id"], status=phase, phase=phase,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    workstream=report.get("workstream"), job_kind=report.get("job_kind"))
    observation._atomic_json(path, previous)


def native_destination(plan_path: Optional[Path], simulator_udid: Optional[str]) -> Dict[str, Any]:
    """Read the plan's v2 destination, retaining the CLI's simulator selection check."""
    if plan_path is None:
        raise BlockedError("native readiness plan is required")
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BlockedError(f"cannot read native readiness plan: {error}") from error
    if isinstance(plan, dict) and plan.get("version") == 1:
        if simulator_udid is None:
            raise BlockedError("version 1 native plans require a simulator UUID")
        return {"simulator": simulator_udid}
    destination = plan.get("destination") if isinstance(plan, dict) else None
    if isinstance(destination, dict) and set(destination) == {"simulator"} and simulator_udid is None:
        raise BlockedError("simulator native plans require --simulator-udid so the device lease is held")
    if simulator_udid is not None and destination != {"simulator": simulator_udid}:
        raise BlockedError("native plan destination does not match --simulator-udid")
    if not isinstance(destination, dict):
        raise BlockedError("native plan must declare a destination")
    return destination


def native_gate(options, report, worktree, sandbox, verify_env, secrets, deadline, wrapper=None):
    directory = Path(report["report_dir"])
    progress(report, "native_preflight")
    gate_started = time.monotonic()
    counter = 0
    def confined(argv):
        nonlocal counter
        counter += 1
        wrapped = verification_argv(sandbox, options.codex_bin, worktree, argv)
        # Native steps build, install and run what the model wrote: same deny wrapper.
        if wrapper is not None:
            wrapped = wrapper.wrap(wrapped)
        env = full_host_env(verify_env)
        result = run_bounded(wrapped, cwd=worktree, env=env,
                             timeout=deadline.require("native preflight command"))
        output = redact((result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace"), secrets)
        if wrapper is not None:
            wrapper.collect(output, f"native step {counter}")
        log = directory / f"native-command-{counter}.log"
        log.write_text(output, encoding="utf-8")
        report["logs"].append(str(log))
        # All evidence returned to native qualification is sanitized too.
        return dict(result, stdout=redact(result["stdout"].decode("utf-8", "replace"), secrets),
                    stderr=redact(result["stderr"].decode("utf-8", "replace"), secrets))
    try:
        destination = native_destination(options.native_preflight_plan,
                                         options.apple_simulator_udid)
        evidence = native.qualify(options.native_preflight_plan, worktree, directory,
                                  destination, confined, deadline)
    except native.NativeReadinessError as error:
        report["native_preflight"] = error.evidence
        raise BlockedError("end-to-end native preflight failed: " + str(error)) from error
    finally:
        report["native_preflight_seconds"] = round(time.monotonic() - gate_started, 3)
    report["native_preflight"] = evidence


def _execute(options: JobOptions, report: Dict[str, Any], deadline: Deadline,
             lease: Optional["pool.Lease"] = None,
             secrets: Optional[Sequence[str]] = None) -> None:
    task = options.task
    is_cloud = task.tier.kind == "cloud"
    state_dir = options.state_dir
    if _inside(state_dir, task.repo_path):
        raise ContractError(
            f"state dir {state_dir} must live outside the repository {task.repo_path}; "
            "reports and logs must not be worker-writable"
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    # The report directory exists before anything can fail, so preflight and policy
    # refusals are persisted outside the worktree instead of vanishing with the process.
    report_dir = ensure_report_dir(options, report)
    if report_dir is None:
        raise BlockedError(f"cannot create a report directory under {state_dir}")
    lease_update(lease, report_dir=str(report_dir))
    progress(report, "model_preflight")
    deadline.require("preflight")
    # The user's permissions list is local configuration read before anything else is spent on
    # the job: a list that cannot be used, or that would hide what the job needs, refuses it.
    perms = load_job_permissions(options, report_dir)
    # The installed Codex must run the qualified invocation before anything else is spent on
    # the job, a model load included.
    report["codex_compat"] = codex_compat(options.codex_bin, run=options.codex_run)
    if not report["codex_compat"]["ok"]:
        raise CodexIncompatible(report["codex_compat"])
    if options.load_model and is_cloud:
        raise ContractError("--load is available only for local-tier jobs")
    if options.load_model:
        if not options.requested_model:
            raise ContractError("--load requires --model")
        load_timeout = deadline.require("model load")
        report["model_load"] = load_requested_model(options.settings_path,
                                                    options.requested_model, options.omlx_fetch,
                                                    timeout=load_timeout)
    selected_cloud = None
    if is_cloud:
        try:
            catalog_models = codex_catalog.list_cloud_models(options.codex_bin, run=options.codex_run)
        except RuntimeError as error:
            raise BlockedError(str(error)) from error
        selected_cloud = next((model for model in catalog_models if model.slug == task.tier.model), None)
        if selected_cloud is None:
            raise BlockedError("requested cloud model is not present in the live Codex catalog")
        effort = task.tier.reasoning_effort
        if effort is not None and effort not in selected_cloud.reasoning_levels:
            raise ContractError("tier.reasoning_effort is not supported by the selected cloud model")
        model = omlx_discovery.ModelProfile(
            canonical_id=selected_cloud.slug, api_model=selected_cloud.slug, model_type="llm",
            engine_type=None, context_window=selected_cloud.context_window,
            max_output_tokens=None, accepts_images=True, thinking_default=None,
            reasoning_efforts=list(selected_cloud.reasoning_levels))
        profile = omlx_discovery.ServerProfile(
            base_url="", version=None, model=model, prefill_tps=None, generation_tps=None,
            server_prompt_tokens=None, active_requests=None,
            discovered_at=datetime.now(timezone.utc).isoformat())
        context_window = (min(selected_cloud.context_window, options.context_cap)
                          if options.context_cap else selected_cloud.context_window)
        report["preflight"] = {"tier": "cloud", "source": "codex debug models",
                               "model": selected_cloud.slug}
        report["endpoint"] = None
        report["model_profile"] = {"cloud": selected_cloud.__dict__}
        report["cloud_catalog"] = {"source": "codex debug models", "visibility": "list"}
        api_key = ""
    else:
        profile, context_window = discover_job_profile(
            options.settings_path, options.requested_model, options.context_cap, options.omlx_fetch)
        report["preflight"] = preflight_receipt(profile, context_window)
        report["endpoint"] = report["preflight"]["endpoint"]
        report["model_profile"] = report["preflight"]["profile"]
        api_key = read_api_key(options.settings_path)
    report["context_window"] = context_window
    report["api_model"] = profile.model.api_model
    # The caller's list already holds the credentials selected by the tools manifest: it grows
    # here instead of being reset, so nothing collected before the first failure is forgotten.
    if secrets is None:
        secrets = []
    if api_key and api_key not in secrets:
        secrets.append(api_key)

    if options.apple_simulator_udid is not None and options.native_preflight_plan is None:
        raise BlockedError("--simulator-udid requires --native-preflight-plan")
    if options.preflight_only and options.native_preflight_plan is None:
        raise BlockedError("native --preflight-only requires a native plan")
    if options.native_preflight_plan is not None:
        native_destination(options.native_preflight_plan, options.apple_simulator_udid)
        if not options.verify:
            raise BlockedError("native jobs require verification; --no-verify cannot bypass native qualification")
    base_commit = resolve_commit(task.repo_path, task.base_ref)
    report["base_commit"] = base_commit
    report["dirty_primary_files"] = dirty_primary_files(task.repo_path)

    # Captured before the worktree exists and before anything can run: filenames alone are
    # not enough, because a path that was already dirty keeps its name when it is edited
    # again, so the after-the-job comparison also hashes the contents of every dirty path.
    report["primary_fingerprint_before"] = primary_fingerprint(task.repo_path)

    worktree = state_dir / "worktrees" / worktree_name(task.task_id)
    report["worktree"] = str(worktree)
    add_worktree(task.repo_path, report["base_commit"], worktree)
    report["worktree_preserved"] = True
    lease_update(lease, worktree=str(worktree))

    # Read the worktree's git layout now, while the gitfile and commondir are still the launcher's
    # own (the worker has not run), and fingerprint the files that could repoint them. Every later
    # (unwrapped) audit pins this layout, and any change to the fingerprint is an audit violation.
    git_layout = worktree_git_layout(worktree)
    report["_git_layout"] = git_layout
    report["_git_admin_before"] = git_admin_fingerprint(git_layout, worktree)

    # The job's own Codex home (local) or native authenticated Codex home (cloud), and the model
    # catalog generated for the pinned local model. The catalog
    # sits in the report directory, which the worker cannot write, not in its writable Codex home;
    # the hash write_catalog returns is what every launch self-check compares the file against.
    if is_cloud:
        source_home = resolve_cloud_codex_home()
        if (_inside(source_home, task.repo_path) or _inside(task.repo_path, source_home)
                or _inside(source_home, state_dir) or _inside(state_dir, source_home)):
            raise BlockedError("cloud CODEX_HOME must not overlap the repository or launcher state")
        codex_home = source_home
        auth_observer = CloudAuthObservation(source_home, report_dir, secrets)
        report["_cloud_auth"] = auth_observer
        report["cloud_auth"] = {
            "credential_source": "native CODEX_HOME",
            "home_isolated": False,
            "source_home": str(source_home),
            "auth_file": str(source_home / "auth.json"),
            "visible_helper_scratch": str(source_home / "tmp" / "arg0-macos"),
            "auth_management": "Codex CLI native locking and refresh",
            "initial_credentials_observed": auth_observer.observe_initial(),
        }
    else:
        codex_home = job_codex_home(report_dir)
    report["codex_home"] = str(codex_home)
    # The roots the wrapper re-opens and rename-protects must exist before the profile is built:
    # a protected path that is missing cannot be created inside the job (xcodebuild's
    # -derivedDataPath, a screenshot capture).
    create_native_roots(report_dir)
    catalog_path = None
    catalog = None
    if not is_cloud:
        catalog_path = report_dir / codex_catalog.CATALOG_NAME
        try:
            catalog_sha = codex_catalog.write_catalog(
                catalog_path, codex_catalog.build_local_entry(profile.model, context_window))
        except (OSError, ValueError) as error:
            raise BlockedError(f"cannot generate the model catalog {catalog_path}: {error}") from error
        report["catalog_sha256"] = catalog_sha
        catalog = (catalog_path, catalog_sha)

    if is_cloud:
        env, stripped_proxies, cloud_names = cloud_worker_env(codex_home, task.environment_pass)
    else:
        env, stripped_proxies, cloud_names = worker_env(
            api_key, codex_home, task.environment_pass)
    if options.native_preflight_plan is not None:
        # Native toolchain pin for the worker itself: ibtool/actool run in-process. HOME stays
        # exactly as the job had it, so the real simctl/Xcode state is used; CODEX_HOME is the
        # job's own home like for every other job.
        env["IBToolNeverDeque"] = "1"

    # Task tools are opt-in. The loader only ever sees the credentials the manifest names, and
    # only the ones the caller actually has; a refused manifest blocks the job rather than
    # quietly running without the tools the task asked for.
    selected = [name for name in tools.selected_env_names(options.tool_config) if name in os.environ]
    loader_env = dict(env)
    for name in selected:
        loader_env[name] = os.environ[name]
    try:
        loaded = tools.load_tool_config(options.tool_config, loader_env, worktree)
    except tools.ToolConfigError as error:
        raise BlockedError(f"tools configuration refused: {error}") from error
    # Every credential the loader resolved joins the caller's list before the first tool log,
    # so the shared list redacts the report, the JSONL, stderr and the patch alike.
    secrets.extend(value for value in loaded["secrets"].values() if value not in secrets)
    report["tooling"] = loaded["evidence"]
    env.update(loaded["environment"])
    report["environment"] = {
        "proxies_stripped": stripped_proxies,
        "cloud_credential_vars_removed": cloud_names,
        "api_key_env": API_KEY_ENV,
        "shell_env_excludes": list(SHELL_ENV_EXCLUDES),
        "codex_home": env["CODEX_HOME"],
    }
    if options.native_preflight_plan is not None:
        report["environment"]["native_env"] = {
            "IBToolNeverDeque": env.get("IBToolNeverDeque"),
            "destination": native_destination(options.native_preflight_plan,
                                               options.apple_simulator_udid),
        }

    credentials = set(selected) | set(loaded["environment"])
    verify_env = {name: value for name, value in env.items()
                  if name != API_KEY_ENV and name not in credentials}
    # Required tools are confirmed before any inference, with the installed Codex MCP client and
    # no model turn: the probe sends only initialize, initialized and mcpServerStatus/list. A
    # server that cannot list a configured tool blocks the job instead of spending tokens on a
    # worker that would silently improvise the missing capability.
    try:
        report["tooling"] = probe_tools(
            loaded, codex_bin=options.codex_bin,
            env={name: value for name, value in env.items() if name != API_KEY_ENV},
            overrides=(cloud_config_overrides(task.tier.reasoning_effort) if is_cloud else
                config_overrides(profile, context_window, catalog_path,
                sandbox_mode="read-only" if task.kind == "read" else REQUIRED_SANDBOX_MODE)),
            deadline=deadline, codex_home=codex_home)
    except ToolProbeError as error:
        raise BlockedError(f"required MCP tools could not be probed: {error}") from error
    # Self-check the pinned policy before the first subprocess: drift is a refusal, not a
    # degraded run. Read jobs add Codex's read-only sandbox and generated output schema.
    if task.kind == "read":
        read_schema_path = report_dir / "answer.schema.json"
        read_schema_path.write_text(json.dumps(
            read_answers.envelope_schema(task.answer_schema or {}), indent=2) + "\n",
            encoding="utf-8")
        read_argv = worker_argv_for_tier(
            options, worktree, profile, context_window,
            report_dir / "policy-check.last-message.txt", loaded["overrides"],
            list(loaded["environment"]), catalog_path, sandbox_mode="read-only",
            output_schema=read_schema_path)
        if is_cloud:
            problems = cloud_policy_check(read_argv, task.tier.model or "",
                                          task.tier.reasoning_effort, sandbox_mode="read-only")
            if problems:
                raise BlockedError("cloud worker policy self-check refused the launch: " + "; ".join(problems))
        else:
            _policy_check(read_argv, profile, context_window, catalog,
                          sandbox_mode="read-only", output_schema=read_schema_path)
    else:
        argv = worker_argv_for_tier(options, worktree, profile, context_window,
            report_dir / "policy-check.last-message.txt", loaded["overrides"],
            list(loaded["environment"]), catalog_path)
        if is_cloud:
            problems = cloud_policy_check(argv, task.tier.model or "",
                                          task.tier.reasoning_effort)
            if problems:
                raise BlockedError("cloud worker policy self-check refused the launch: " + "; ".join(problems))
        else:
            _policy_check(argv, profile, context_window, catalog)
    # Every process that runs model-influenced code from here on (the worker, each check, each
    # native step) runs behind the job's deny wrapper; the self-check above saw the inner argv.
    wrapper = setup_deny_wrapper(options, report, perms, report_dir, worktree, codex_home,
                                 git_layout, verify_env, secrets, deadline)
    sandbox = probe_sandbox(options.codex_bin, worktree, verify_env, secrets, deadline)
    if not options.verify:
        sandbox = dict(sandbox)
        sandbox["verification"] = "skipped by flag"
    report["sandbox"] = sandbox
    if options.native_preflight_plan is not None:
        sandbox = _apple_opt_in(options, report, report_dir, worktree, sandbox, verify_env,
                                secrets, deadline)
    sandbox = dict(sandbox, deny_list_applied=wrapper.applied)
    if wrapper.applied and sandbox.get("backend") == FULL_HOST_BACKEND:
        sandbox["confinement"] = FULL_HOST_DENY_CONFINEMENT
    report["sandbox"] = sandbox

    if task.kind == "read":
        if options.native_preflight_plan is not None:
            raise BlockedError("read jobs cannot opt into native build or simulator execution")
        sandbox["verification"] = "not run for read jobs"
        report["sandbox"] = sandbox
        _execute_read_job(options, report, deadline, profile, context_window, worktree,
                          report_dir, env, loaded, catalog_path, catalog, wrapper, secrets,
                          report.get("_policy"))
        return

    if options.native_preflight_plan is not None:
        native_gate(options, report, worktree, sandbox, verify_env, secrets, deadline, wrapper)
        if options.preflight_only:
            report["status"] = "ready"
            report["qualification_only"] = True
            report["primary_fingerprint_after"] = primary_fingerprint(task.repo_path)
            report["primary_unchanged"] = report["primary_fingerprint_after"] == report["primary_fingerprint_before"]
            report["primary_changed_during_job"] = not report["primary_unchanged"]
            if not report["primary_unchanged"] and not wrapper.applied:
                report["status"] = "audit_violation"
            return

    # The prompt is built per attempt: only here is the effective worker cutoff known, and a
    # retry must see the budget the earlier attempt and verification actually consumed.
    feedback: Optional[str] = None
    worker_ok = False
    model_blocked = False
    tests: List[Dict[str, Any]] = []
    audit: Optional[Audit] = None
    stream = report.get("_policy")
    maximum_attempts = min(task.max_attempts, report.get("workstream_admission", {}).get("max_attempts", task.max_attempts))
    # Per-path state fingerprints the worker itself authored, accumulated across attempts.
    # Internal evidence only: it is removed before the report is written.
    worker_authored: Dict[str, str] = {}
    report["_worker_authored"] = worker_authored
    attempt_number = 1
    while attempt_number <= maximum_attempts:
        try:
            if stream is not None:
                stream.before_attempt(report["job_id"])
            remaining = deadline.require(f"attempt {attempt_number}")
        except policy.PolicyStop as error:
            report["failure_reason"] = str(error)
            report["policy_stop"] = True
            report["policy_stop_next_action"] = error.next_action
            break
        except DeadlineExpired as error:
            report["attempts"].append({"attempt": attempt_number, "skipped": str(error)})
            report["failure_reason"] = (
                f"the {deadline.total_seconds:.0f}s total job budget was exhausted before "
                f"attempt {attempt_number}")
            break
        # The job is pinned to one model, so every attempt must still find it served as pinned:
        # a model swapped in oMLX mid-job would otherwise mix results from two models. This is
        # after the cheap local admission checks and before the attempt's budget is computed,
        # because the request itself spends some of it. A non-``None`` answer ends the job
        # as blocked; the attempts that already ran stay audited and preserved below.
        changed = (None if is_cloud else
                   recheck_pinned_model(profile, options.settings_path, options.omlx_fetch,
                                        attempt_number))
        if changed is not None:
            reason, message = changed
            report["attempts"].append({"attempt": attempt_number, "skipped": message,
                                       "skipped_reason": reason})
            report["failure_reason"] = message
            model_blocked = True
            break
        remaining = deadline.remaining()
        reserve = min(60.0, max(5.0, deadline.total_seconds * .20)) if options.verify else 0.0
        if remaining <= reserve + 1:
            report["failure_reason"] = "worker admission stopped to preserve independent verification time"
            break
        attempt_timeout = deadline.cap(remaining - reserve)
        stall = stall_limit(profile, context_window)
        prompt = build_prompt(task, worktree, feedback,
                              apple_udid=options.apple_simulator_udid, deadline=deadline,
                              attempt_timeout=attempt_timeout, reserve=reserve)
        if loaded["prompt"]:
            prompt += "\n" + loaded["prompt"] + "\n"

        jsonl_path = report_dir / f"worker-attempt-{attempt_number}.jsonl"
        prompt_path = report_dir / f"attempt-{attempt_number}.prompt.txt"
        stderr_path = report_dir / f"worker-attempt-{attempt_number}.stderr.log"
        last_path = last_message_path(report_dir, attempt_number)
        prompt_path.write_text(redact(prompt, secrets), encoding="utf-8")
        argv = worker_argv_for_tier(options, worktree, profile, context_window, last_path,
                                    loaded["overrides"], list(loaded["environment"]), catalog_path)
        if is_cloud:
            problems = cloud_policy_check(argv, task.tier.model or "", task.tier.reasoning_effort)
            if problems:
                raise BlockedError("cloud worker policy self-check refused the launch: " + "; ".join(problems))
        else:
            _policy_check(argv, profile, context_window, catalog)
        before_worker = audit_worktree(worktree, base_commit, task.allowed_paths, secrets, scratch_dir=report_dir, git_layout=git_layout,
                                       git_admin_before=report["_git_admin_before"])
        patch_before = before_worker.patch
        # Snapshot every changed-from-base path *before* the worker runs, so whatever state a
        # path is in afterwards can be attributed to this attempt only if it actually changed.
        before_states = {path: path_state(worktree / path) for path in before_worker.changed_paths}
        observer = observation.RunObserver(report_dir, secrets=secrets,
                                           attempt=attempt_number, job_id=report["job_id"],
                                           active_dir=state_dir / "active",
                                           worker_command=options.codex_bin,
                                           job={"started_at": report.get("started_at"),
                                                "workstream": report.get("workstream"),
                                                "job_kind": report.get("job_kind")})
        context_token = _RUN_OBSERVER.set(observer)
        try:
            if stream is not None:
                stream.start_attempt(report["job_id"])
            report["_active_attempt"] = {"before_patch": patch_before}
            # Bound a synchronous busy-server check by both this attempt's remaining
            # allowance and the job cutoff that preserves verification time.
            attempt_deadline = min(deadline.end - reserve,
                                   time.monotonic() + attempt_timeout)
            runner_timeout = max(0.0, attempt_deadline - time.monotonic())
            result = run_bounded(wrapper.wrap(argv), cwd=worktree, env=env,
                                 timeout=runner_timeout, stdin_data=prompt.encode("utf-8"),
                                 stall_seconds=stall["seconds"],
                                 on_stall=(None if is_cloud else
                                     lambda: server_busy(options.settings_path,
                                                         options.omlx_fetch,
                                                         attempt_deadline)))
        except BaseException:
            observer.finish(status="failed")
            snapshot = observer.snapshot()
            report["attempts"].append({"attempt": attempt_number, "returncode": None,
                "timed_out": False, "interrupted": True,
                "elapsed_seconds": snapshot["usage"]["elapsed_seconds"],
                "usage": snapshot["usage"]})
            raise
        finally:
            _RUN_OBSERVER.reset(context_token)
        # The real streaming runner already fed each chunk live. A test/injected runner may
        # return captured bytes without invoking the observer callback, so complete the same
        # accumulation and persisted stream once in that compatibility path.
        if observer.activity_count == 0:
            if result.get("stdout"):
                observer("stdout", result["stdout"])
            if result.get("stderr"):
                observer("stderr", result["stderr"])
        observer.finish(result=result)
        stdout_text = redact(result["stdout"].decode("utf-8", "replace"), secrets)
        stderr_text = redact(result["stderr"].decode("utf-8", "replace"), secrets)
        for text in [*worker_command_outputs(stdout_text), stderr_text]:
            wrapper.collect(text, f"worker attempt {attempt_number}")
        evidence = parse_worker_log(stdout_text)
        report["tooling"] = tools.mark_observations(report.get("tooling"), stdout_text)
        routing = model_routing(evidence, report["api_model"])
        timeline = observer.snapshot()["usage"]
        for field_name in ("started_at", "finished_at", "elapsed_seconds"):
            evidence["usage"][field_name] = timeline[field_name]
        if (result.get("timed_out") or result.get("returncode") != 0
                or result.get("output_limit_exceeded") or evidence["terminal_failures"]):
            evidence["usage"]["complete"] = False
            if evidence["usage"]["availability"] == "complete":
                evidence["usage"]["availability"] = "partial"
        # Check actual scoped changes BEFORE verification can create its own files.
        after_worker = audit_worktree(worktree, base_commit, task.allowed_paths, secrets, scratch_dir=report_dir, git_layout=git_layout,
                                      git_admin_before=report["_git_admin_before"])
        after_states = {path: path_state(worktree / path) for path in after_worker.changed_paths}
        # A changed path was authored by this attempt's worker only when its state differs from
        # the before-worker snapshot; a path that was not changed before counts as authored.
        authored = {path: state for path, state in after_states.items()
                    if before_states.get(path) != state}
        has_patch = bool(authored) and not after_worker.violations
        if has_patch:
            worker_authored.update(authored)
        # Drop any retained fingerprint the tree no longer matches: reverted, rewritten by
        # verification, or deleted, the worker's provenance for that path is no longer provable.
        for path in list(worker_authored):
            if after_states.get(path) != worker_authored[path]:
                del worker_authored[path]
        # Success has to be earned by the worker, not by a verification command that happens to
        # write a file: this is the only place the flag is set, and it is always a bool.
        report["worker_patch_present"] = bool(after_worker.patch) and bool(worker_authored)
        report.pop("_active_attempt", None)
        last_message, last_message_missing, last_message_tampered = safe_read_job_text(
            last_path, report_dir, secrets)
        if not last_path.exists():
            last_path.write_text("", encoding="utf-8")
        attempt = {
            "attempt": attempt_number,
            "returncode": result["returncode"],
            "timed_out": result["timed_out"],
            "stalled": bool(result.get("stalled")),
            "output_limit_exceeded": bool(result.get("output_limit_exceeded")),
            "elapsed_seconds": result["elapsed_seconds"],
            "verification_reserve_seconds": reserve,
            "worker_timeout_seconds": attempt_timeout,
            "limits": {"stall": stall, "budget_seconds": attempt_timeout},
            "has_scoped_patch": has_patch,
            "usage": evidence["usage"],
            "termination": result["termination"],
            "observed_models": evidence["observed_models"],
            "model_routing": routing,
            "tool_call_events": evidence["tool_call_events"],
            "tool_started_count": evidence["tool_started_count"],
            "tool_completed_count": evidence["tool_completed_count"],
            "thread_id": evidence["thread_id"],
            "unsupported_tools": evidence["unsupported_tools"],
            "terminal_failures": evidence["terminal_failures"],
            "error_text": typed_worker_error_text(stdout_text),
            "token_counts": evidence["token_counts"],
            "stderr_excerpt": _tail(stderr_text),
            "last_message_excerpt": _tail(last_message),
            "logs": [str(prompt_path), str(jsonl_path), str(stderr_path), str(last_path)],
        }
        if last_message_missing:
            attempt["last_message_missing"] = last_message_missing
        if last_message_tampered:
            attempt["last_message_tampered"] = last_message_tampered
        report["logs"].extend(attempt["logs"])
        if evidence["unsupported_tools"]:
            attempt["failure_hint"] = "worker reported an unsupported tool; stopping without any model switch"
            report["attempts"].append(attempt)
            if stream is not None:
                report["workstream_progress"] = stream.record_attempt(
                    report["job_id"], capability_failure=False, has_patch=has_patch)
            report["failure_reason"] = attempt["failure_hint"]
            break
        if result.get("output_limit_exceeded"):
            attempt["failure_hint"] = (
                f"worker output exceeded the {observation.CAPTURE_LIMIT_BYTES} byte capture "
                "limit, so only an incomplete prefix of stdout and stderr was ever seen and "
                "the run cannot be judged from truncated output; the termination record "
                "separately states what was done to the process group")
            report["attempts"].append(attempt)
            if stream is not None:
                report["workstream_progress"] = stream.record_attempt(
                    report["job_id"], capability_failure=False, has_patch=has_patch)
            report["failure_reason"] = attempt["failure_hint"]
            break
        report["attempts"].append(attempt)

        if options.verify:
            progress(report, "verification")
            tests = run_verification(task, worktree, verify_env, options.codex_bin, sandbox,
                                     secrets, deadline, wrapper)
            for test in tests:
                text = test.pop("output_text", "")
                # Keep the evidence for anything that actually ran, confined or not: an
                # executed full-host command is the primary evidence of a full-host job.
                if test.get("status") not in ("passed", "failed"):
                    continue
                wrapper.collect(text, f"verification attempt {attempt_number} command {test['log']}")
                log_path = report_dir / f"verify-attempt-{attempt_number}-{test['log']}.log"
                try:
                    log_path.write_text(text, encoding="utf-8")
                    test["log_path"] = str(log_path)
                    report["logs"].append(str(log_path))
                except OSError:
                    test["log_path"] = None
                if len(text) > len(str(test.get("stdout_tail", ""))):
                    test["output_truncated_in_report"] = True
        else:
            tests = [{"command": argv, "sandboxed": False, "status": "skipped", "returncode": None}
                     for argv in task.test_commands]
        attempt["tests"] = tests
        report["tests"] = tests

        # The audit runs *after* verification on every attempt: a test command can itself
        # write files, and the comparison target is the original base commit, so committed,
        # staged, unstaged and untracked work is all visible to it.
        audit = audit_worktree(worktree, base_commit, task.allowed_paths, secrets,
                               scratch_dir=report_dir, git_layout=git_layout,
                               git_admin_before=report["_git_admin_before"])
        report["head_commit"] = audit.head_commit
        report["changed_paths"] = audit.changed_paths
        report["violations"] = audit.violations

        worker_ok = (result["returncode"] == 0 and not result["timed_out"]
                     and not result.get("stalled")
                     and not result.get("output_limit_exceeded")
                     and routing != "mismatch" and not evidence["terminal_failure"])
        tests_ok = (all(test.get("status") == "passed" for test in tests) if options.verify else True)
        candidate_status = ("audit_violation" if audit.violations else
                            "tests_failed" if worker_ok and not tests_ok else
                            "no_changes" if worker_ok and tests_ok and not report.get("worker_patch_present") else
                            "worker_failed")
        previous_codes = [item.get("code") for item in report.get("auto_retries", [])
                          if isinstance(item, dict) and isinstance(item.get("code"), str)]
        classification = failure_classify.classify(dict(
            report, status=candidate_status, context_window=context_window,
            previous_codes=previous_codes))
        if stream is not None:
            report["workstream_progress"] = stream.record_attempt(
                report["job_id"], capability_failure=(classification.cls == "capability"),
                has_patch=has_patch)
        if routing == "mismatch" or (worker_ok and tests_ok and not audit.violations):
            break
        fix = classification.auto_fix
        if (classification.next_action == "retry_local"
                and isinstance(fix, dict) and deadline.remaining() > reserve + MIN_ATTEMPT_BUDGET_SECONDS):
            applied_retry = False
            if isinstance(fix.get("context_cap"), int) and fix["context_cap"] < context_window:
                context_window = fix["context_cap"]
                report["context_window"] = context_window
                report["auto_retries"].append({"code": classification.code,
                                               "context_window": context_window})
                applied_retry = True
            elif isinstance(fix.get("wait_for_server"), (int, float)):
                wait_seconds = min(float(fix["wait_for_server"]),
                                   max(0.0, deadline.remaining() - reserve - MIN_ATTEMPT_BUDGET_SECONDS))
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                    report["auto_retries"].append({"code": classification.code,
                                                   "wait_for_server": wait_seconds})
                    applied_retry = True
            else:
                wait_seconds = 0
            if applied_retry:
                maximum_attempts += 1
                feedback = summarize_failure(attempt, tests, audit.violations)
                attempt_number += 1
                continue
        if attempt_number < maximum_attempts:
            feedback = summarize_failure(attempt, tests, audit.violations)
        attempt_number += 1

    if audit is None:
        # No attempt reached verification (unsupported tool, exhausted budget, ...). Audit
        # what the worker left behind anyway, still against the original base commit.
        audit = audit_worktree(worktree, base_commit, task.allowed_paths, secrets,
                               scratch_dir=report_dir, git_layout=git_layout,
                               git_admin_before=report["_git_admin_before"])
        report["head_commit"] = audit.head_commit
    patch_path = report_dir / "changes.patch"
    patch_path.write_bytes(audit.patch)
    report["patch_path"] = str(patch_path)
    report["patch_bytes"] = len(audit.patch)
    report["patch_excerpt"] = audit.patch[:PATCH_REPORT_BYTES].decode("utf-8", "replace")
    report["diffstat"] = audit.diffstat
    report["changed_paths"] = audit.changed_paths
    report["violations"] = audit.violations
    note_git_admin_tamper(report, worktree)
    report["ignored_paths"] = audit.ignored_paths
    # Final provenance check, after verification and the final audit: the flag is true only
    # while a worker-authored path still exists in its authored state. No final patch means no
    # patch, and a leftover file from an earlier verification command proves nothing.
    final_states = {path: path_state(worktree / path) for path in audit.changed_paths}
    for path in list(worker_authored):
        if final_states.get(path) != worker_authored[path]:
            del worker_authored[path]
    report["worker_patch_present"] = bool(audit.patch) and bool(worker_authored)
    report["dirty_primary_after"] = dirty_primary_files(task.repo_path)
    report["primary_fingerprint_after"] = primary_fingerprint(task.repo_path)
    report["primary_unchanged"] = (report["primary_fingerprint_after"]
                                   == report["primary_fingerprint_before"])
    report["primary_changed_during_job"] = not report["primary_unchanged"]
    left = deadline.remaining()
    report["budget"] = {
        "total_seconds": deadline.total_seconds,
        "elapsed_seconds": round(deadline.total_seconds - max(left, 0.0), 2),
        "remaining_seconds": round(left, 2),
        "exceeded": deadline.exceeded(),
    }

    attempts = [item for item in report["attempts"] if "returncode" in item]
    report.update(observation.sum_usage(attempts))

    report["status"] = ("blocked" if report.get("policy_stop") or model_blocked
                        else decide_status(report, worker_ok))


def decide_status(report: Dict[str, Any], worker_ok: bool) -> str:
    """Never report success without passing verification evidence."""
    attempts = [item for item in report["attempts"] if "returncode" in item]
    statuses = [test.get("status") for test in report["tests"]]
    all_passed = bool(statuses) and all(status == "passed" for status in statuses)
    budget_exceeded = bool((report.get("budget") or {}).get("exceeded"))
    # Without the deny wrapper a changed primary checkout is a policy violation no matter what
    # else happened, checked first so it can never be reported as success. With the wrapper
    # applied the worker could not write it, so the change came from the parent or the user: it
    # is reported (``primary_changed_during_job``) and not attributed (spec §6.10).
    if report.get("primary_unchanged") is False and not deny_list_applied(report):
        return "audit_violation"
    if report["violations"]:
        return "audit_violation"
    if any(item.get("model_routing") == "mismatch" for item in attempts):
        return "model_mismatch"
    if any(item.get("unsupported_tools") for item in attempts):
        return "worker_failed"
    # A documented terminal event ends the run unsuccessfully even when the process exited 0,
    # but only the attempt that actually ran last decides the verdict: a failed turn that a
    # clean retry recovered from stays in the attempt history instead of failing the job.
    if attempts and attempts[-1].get("terminal_failures"):
        return "worker_failed"
    if budget_exceeded and not all_passed:
        return "timeout"
    if "blocked" in statuses:
        return "verification_blocked"
    if not worker_ok:
        return "timeout" if any(item.get("timed_out") for item in attempts) else "worker_failed"
    if "failed" in statuses:
        return "tests_failed"
    if not statuses or any(status != "passed" for status in statuses):
        return "verification_blocked"
    # Every failure signal above outranks this: a clean worker, passing tests and no patch of
    # the worker's own is a no-op, and verifier-written files never count as one. The field is
    # absent only in synthetic legacy reports, which stay on the success path.
    if report.get("worker_patch_present") is False:
        return "no_changes"
    return "ok"


STATUS_REASONS = {
    "contract_invalid": "the task contract was rejected",
    "preflight_failed": "oMLX discovery failed: settings, endpoint or loaded model",
    "blocked": "the job was blocked before the worker could run",
    "worker_failed": "the worker exited unsuccessfully without passing verification",
    "audit_violation": "the worker changed something outside allowed_paths or touched the "
                       "primary checkout",
    "tests_failed": "verification commands failed after the worker finished",
    "verification_blocked": "verification produced no passing evidence",
    "timeout": "the total job budget ran out before passing verification",
    "model_mismatch": "the worker was served by a model other than the pinned one",
    "no_changes": "the worker produced no in-scope implementation patch that survived to the "
                  "final audit; baseline verification passed, and parent review is required",
    "answer_invalid": "the read worker's output failed schema or citation verification",
}


def describe_failure(report: Dict[str, Any]) -> str:
    """Every non-ok status gets a concrete reason, not just a status word."""
    status = report.get("status")
    if status == "ok":
        return ""
    if report.get("failure_reason"):
        return str(report["failure_reason"])
    parts: List[str] = []
    if report.get("primary_unchanged") is False and not deny_list_applied(report):
        parts.append("the primary checkout changed during the job: HEAD, index or the content "
                     "of a dirty file differs from before the job started, and the deny list "
                     "was not applied, so the worker cannot be ruled out")
    for violation in report.get("violations", [])[:5]:
        parts.append(f"audit violation {violation.get('path')} ({violation.get('reason')})")
    for attempt in report.get("attempts", []):
        if attempt.get("model_routing") == "mismatch":
            parts.append(f"attempt {attempt.get('attempt')} observed model(s) "
                         f"{attempt.get('observed_models')} instead of the pinned model "
                         f"{report.get('api_model')}")
        if attempt.get("unsupported_tools"):
            detail = attempt.get("stderr_excerpt") or "the worker stream reported an unsupported tool"
            parts.append(f"attempt {attempt.get('attempt')} reported unsupported tool(s): {detail}")
        if attempt.get("terminal_failures"):
            parts.append(f"attempt {attempt.get('attempt')} ended on terminal event(s) "
                         f"{attempt.get('terminal_failures')} regardless of exit code "
                         f"{attempt.get('returncode')}")
        if attempt.get("output_limit_exceeded"):
            parts.append(f"attempt {attempt.get('attempt')} produced more than "
                         f"{observation.CAPTURE_LIMIT_BYTES} bytes of output: the process group "
                         "was killed and the truncated output cannot prove success")
        if attempt.get("skipped"):
            parts.append(f"attempt {attempt.get('attempt')} skipped: {attempt['skipped']}")
        elif attempt.get("timed_out"):
            parts.append(f"attempt {attempt.get('attempt')} was killed after "
                         f"{attempt.get('elapsed_seconds')}s")
        elif attempt.get("returncode") not in (0, None):
            detail = f"; stderr tail: {attempt['stderr_excerpt']}" if attempt.get(
                "stderr_excerpt") else ""
            parts.append(f"attempt {attempt.get('attempt')} exited "
                         f"{attempt.get('returncode')}{detail}")
    for test in report.get("tests", []):
        if test.get("output_limit_exceeded"):
            parts.append(f"verification output for {json.dumps(test.get('command'))} exceeded "
                         f"the {observation.CAPTURE_LIMIT_BYTES} byte capture limit and was "
                         "truncated, so it cannot count as passing")
        if test.get("status") in ("blocked", "failed"):
            reason = test.get("reason") or (f"exit {test.get('returncode')}"
                                            if test.get("returncode") is not None
                                            else "no evidence")
            parts.append(f"verification {test.get('status')} for "
                         f"{json.dumps(test.get('command'))}: {reason}")
    if not parts:
        if not report.get("tests"):
            parts.append("no verification evidence was collected")
        return "; ".join(parts + [STATUS_REASONS.get(str(status), f"status {status}")])
    return "; ".join(parts)


def write_report(report: Dict[str, Any], secrets: Sequence[str]) -> None:
    text = redact(json.dumps(report, indent=2, sort_keys=False), secrets)
    report_dir = report.get("report_dir")
    if report_dir:
        try:
            Path(report_dir).mkdir(parents=True, exist_ok=True)
            (Path(report_dir) / "report.json").write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass


def run_job(options: JobOptions) -> Dict[str, Any]:
    """Run one serialized job and return the structured report."""
    report = new_report(options)
    started = time.monotonic()
    # One budget for the whole job: lock wait, preflight, policy check, sandbox probe,
    # attempts, every verification command, the audit and report writing. Nothing inside a
    # job resets it, so ``timeout_seconds`` is a real upper bound.
    deadline = Deadline(float(options.task.timeout_seconds))
    secrets: List[str] = []
    # Collected before anything can fail: a credential the tools manifest names is redacted even
    # when the job is refused by preflight, policy or the manifest itself.
    secrets.extend(tools.redaction_secrets(options.tool_config, os.environ))
    for name in options.task.environment_pass:
        value = os.environ.get(name)
        if value and len(value) >= 4 and value not in secrets:
            secrets.append(value)
    stream = None
    claimed = False
    try:
        if _inside(options.state_dir, options.task.repo_path):
            raise ContractError(
                f"state dir {options.state_dir} must live outside the repository "
                f"{options.task.repo_path}; reports and logs must not be worker-writable"
            )
        try:
            stream = policy.Workstream(options.state_dir, repo_identity(options.task.repo_path),
                                       options.workstream or options.task.workstream,
                                       options.job_kind, tier=options.task.tier.kind)
            if not options.preflight_only:
                admission = stream.claim(report["job_id"], deadline.require("workstream admission"), options.task.max_attempts)
                claimed = True
                report["workstream_admission"] = admission
                if admission["job_budget_seconds"] < deadline.remaining():
                    deadline.end = min(deadline.end, time.monotonic() + admission["job_budget_seconds"])
        except (policy.PolicyStop, ValueError) as error:
            if isinstance(error, policy.PolicyStop):
                report["policy_stop_next_action"] = error.next_action
            raise BlockedError(str(error)) from error
        report["_policy"] = stream if claimed else None
        key_error: Optional[Exception] = None
        if options.task.tier.kind == "local":
            try:
                model_key = read_api_key(options.settings_path)
                if model_key not in secrets:
                    secrets.append(model_key)
            except PreflightError as error:
                key_error = error
        # Every lock wait is inside the single job budget: it is measured, never reset.
        lock_timeout = min(float(options.lock_timeout),
                           float(options.task.timeout_seconds),
                           max(deadline.remaining(), 0.0))
        with job_lease(options, report, lock_timeout, job_deadline=deadline.end) as lease:
            report["reaped_orphans"] = reap_orphans(options.state_dir)
            if key_error is not None:
                raise key_error
            _execute(options, report, deadline, lease, secrets=secrets)
    except (LauncherError, ContractError) as error:
        report["status"] = error.status
        report["failure_reason"] = redact(str(error), secrets)
        if isinstance(error, PreflightError) and error.code:
            report["discovery_error"] = {"code": error.code,
                                         "message": report["failure_reason"],
                                         "candidates": list(error.candidates)}
    except Exception as error:  # noqa: BLE001 - report, never rethrow raw
        report["status"] = "worker_failed"
        report["failure_reason"] = redact(f"unexpected {error.__class__.__name__}: {error}", secrets)
    finally:
        report.pop("_policy", None)
        auth_observer = report.pop("_cloud_auth", None)
        if auth_observer is not None:
            try:
                report.setdefault("cloud_auth", {}).update(auth_observer.finish())
            except Exception as auth_error:  # report scrub status without losing the job result
                report.setdefault("cloud_auth", {}).update(
                    credential_scrub="unavailable (%s)" % auth_error.__class__.__name__)
        unfinished = report.pop("_active_attempt", None)
        if report.get("worktree") and report.get("primary_fingerprint_before"):
            try:
                final_audit = audit_worktree(Path(report["worktree"]), report["base_commit"], options.task.allowed_paths, secrets, scratch_dir=Path(report["report_dir"]), git_layout=report.get("_git_layout"), git_admin_before=report.get("_git_admin_before"))
                report["changed_paths"] = final_audit.changed_paths
                report["violations"] = final_audit.violations
                note_git_admin_tamper(report, Path(report["worktree"]))
                report["head_commit"] = final_audit.head_commit
                tree = Path(report["worktree"])
                last_states = {path: path_state(tree / path) for path in final_audit.changed_paths}
                authored = report.get("_worker_authored") or {}
                for path in list(authored):
                    if last_states.get(path) != authored[path]:
                        del authored[path]
                if report.get("worker_patch_present") and not (final_audit.patch and authored):
                    # This audit is the last word: a patch that vanished before the report was
                    # written cannot be reported as a patch. Failures below keep their priority.
                    report["worker_patch_present"] = False
                    if report.get("status") == "ok" and options.task.kind != "read":
                        report["status"] = "no_changes"
                if unfinished and claimed:
                    stream.record_attempt(report["job_id"], capability_failure=False,
                        has_patch=bool(final_audit.patch) and final_audit.patch != unfinished["before_patch"]
                        and not final_audit.violations)
                report["primary_fingerprint_after"] = primary_fingerprint(options.task.repo_path)
                report["primary_unchanged"] = report["primary_fingerprint_after"] == report["primary_fingerprint_before"]
                report["primary_changed_during_job"] = not report["primary_unchanged"]
                if final_audit.violations or (not report["primary_unchanged"]
                                              and not deny_list_applied(report)):
                    report["status"] = "audit_violation"
            except (LauncherError, policy.PolicyStop, OSError) as audit_error:
                report["status"] = "audit_violation"
                report["failure_reason"] = "could not complete final preservation audit: " + str(audit_error)
        if claimed and stream is not None:
            try:
                stream.finish(report["job_id"], time.monotonic() - started)
            except (policy.PolicyStop, OSError) as error:
                report["status"] = "blocked"
                report["failure_reason"] = str(error)
        left = deadline.remaining()
        report["budget"] = {
            "total_seconds": deadline.total_seconds,
            "elapsed_seconds": round(deadline.total_seconds - max(left, 0.0), 2),
            "remaining_seconds": round(left, 2),
            "exceeded": deadline.exceeded(),
        }
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["budget"].update(elapsed_seconds=report["elapsed_seconds"],
                                effective_seconds=round(deadline.end - started, 3))
        report["exit_code"] = EXIT_CODES.get(report["status"], 4)
        if report["status"] not in ("ok", "ready") and not report.get("failure_reason"):
            report["failure_reason"] = redact(describe_failure(report), secrets)
        classification = failure_classify.classify(report).as_dict()
        if report.get("policy_stop_next_action"):
            classification["next_action"] = report["policy_stop_next_action"]
        report["classification"] = classification
        report.pop("_worker_authored", None)
        report.pop("_git_layout", None)
        report.pop("_git_admin_before", None)
        ensure_report_dir(options, report)
        attempts = [a for a in report["attempts"] if "returncode" in a]
        report.update(observation.sum_usage(attempts))
        report["timing"] = {"job_seconds": report["elapsed_seconds"],
                            "worker_seconds": round(sum(a["elapsed_seconds"] for a in attempts), 3),
                            "verification_seconds": round(sum(t.get("elapsed_seconds", 0) or 0 for a in attempts for t in a.get("tests", [])), 3),
                            "native_preflight_seconds": report.get("native_preflight_seconds", 0.0)}
        codex_home = report.get("codex_home")
        if codex_home:
            home_path = Path(codex_home)
            expected_home = Path(report["report_dir"]) / CODEX_HOME_NAME
            try:
                if home_path.absolute() == expected_home.absolute() and not home_path.is_symlink():
                    shutil.rmtree(str(home_path))
                    report["codex_home_removed"] = not home_path.exists()
                else:
                    report["codex_home_removed"] = False
            except OSError as cleanup_error:
                report["codex_home_removed"] = False
                report["codex_home_cleanup_error"] = redact(str(cleanup_error), secrets)
        progress(report, report["status"])
        write_report(report, secrets)
    return report


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------
def resolve_environment_value(new_name: str, legacy_name: str, explicit: Optional[str]) -> Optional[str]:
    """Explicit CLI argument wins; else the canonical env name, else the legacy one.

    Without an explicit argument, two *different* nonempty values for the canonical and the
    legacy name are refused instead of guessed: picking one would silently split one shared pool
    (or pick one of two Codex binaries) with no way for the operator to see the choice. Equal
    values are the documented compatibility case and are accepted.
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit)
    canonical = (os.environ.get(new_name) or "").strip()
    legacy = (os.environ.get(legacy_name) or "").strip()
    if canonical and legacy and canonical != legacy:
        raise PreflightError(
            f"{new_name} and legacy {legacy_name} are both set to different values "
            f"({canonical!r} vs {legacy!r}); unset one, make them equal, or pass the CLI argument")
    return canonical or legacy or None


def resolve_codex_bin(explicit: Optional[str] = None) -> str:
    candidate = (resolve_environment_value(CODEX_BIN_ENV, LEGACY_CODEX_BIN_ENV, explicit)
                 or shutil.which("codex"))
    if not candidate:
        raise PreflightError(
            f"Codex CLI not found; pass --codex-bin or set {CODEX_BIN_ENV} "
            f"(legacy {LEGACY_CODEX_BIN_ENV} is still honored)")
    return str(Path(candidate).expanduser())


def resolve_state_dir(explicit: Optional[str] = None) -> Path:
    raw = resolve_environment_value(STATE_ENV, LEGACY_STATE_ENV, explicit) or DEFAULT_STATE_DIR
    return Path(os.path.expanduser(raw)).absolute()


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """Which loaded model a worker runs on; shared by ``run`` and ``preflight``."""
    parser.add_argument("--model", metavar="NAME",
                        help="loaded oMLX model to use, by canonical id or alias (default: the "
                             "only loaded chat model, else the server's default model)")
    parser.add_argument("--context-cap", metavar="TOKENS",
                        help=f"cap the discovered context window at this many tokens, "
                             f"{MIN_CONTEXT_CAP}-{MAX_CONTEXT_CAP}; it only ever lowers the "
                             "server's window")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-worker",
        description="Run one local Codex CLI worker against the loaded loopback oMLX model.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one job from a task contract file")
    run.add_argument("--task-file", required=True, help="JSON task contract")
    run.add_argument("--state-dir",
                     help="reports/logs/worktrees root (env %s, legacy %s; default %s, kept as the "
                          "one shared legacy pool root)"
                          % (STATE_ENV, LEGACY_STATE_ENV, DEFAULT_STATE_DIR))
    run.add_argument("--codex-bin", help=f"Codex CLI path (default PATH or {CODEX_BIN_ENV}, "
                                         f"legacy {LEGACY_CODEX_BIN_ENV})")
    run.add_argument("--lock-timeout", type=float, default=LOCK_TIMEOUT_SECONDS)
    run.add_argument("--max-workers", metavar="N", default="1",
                     help="shared-pool ceiling: 1 (default) keeps the exclusive legacy "
                          "worker lock; 2-4 takes the shared legacy lock plus one of at "
                          "most 4 slot leases in the same state directory")
    run.add_argument("--no-verify", action="store_true",
                     help="do not run verification commands at all")
    run.add_argument("--simulator-udid", "--apple-simulator-udid", dest="apple_simulator_udid",
                     metavar="UUID", default=None,
                     help="select a simulator for a native plan (the old --apple-simulator-udid "
                          "name remains an alias)")
    run.add_argument("--native-preflight-plan", type=Path)
    run.add_argument("--preflight-only", action="store_true", help="qualify native baseline without starting a worker")
    run.add_argument("--workstream", default=None, help="stable scope name shared across retries and repair jobs (default: task_id)")
    run.add_argument("--job-kind", choices=("implementation", "repair"), default="implementation")
    run.add_argument("--escalates", metavar="JOB_ID", help="job ID this cloud escalation continues")
    run.add_argument("--tool-config", type=Path, default=None,
                     help="optional MCP tools manifest for this task: the manifest names the "
                          "credentials it needs and their values never reach argv, evidence or "
                          "the prompt")
    run.add_argument("--settings", default=str(OMLX_SETTINGS))
    add_selection_arguments(run)
    run.add_argument("--load", action="store_true",
                     help="ask oMLX to load --model before discovery (requires --model; the load "
                          "time counts against the job budget)")
    run.add_argument("--json", action="store_true", help="print the full report JSON")

    status = sub.add_parser("status", help="read progress and actual usage without starting a worker")
    status.add_argument("--state-dir")
    status.add_argument("--job-id")
    models = sub.add_parser("models", help="show available local or cloud models")
    models.add_argument("--cloud", action="store_true", help="read the live Codex cloud catalog")
    models.add_argument("--json", action="store_true", help="print model data as JSON")
    models.add_argument("--settings", default=str(OMLX_SETTINGS))
    models.add_argument("--codex-bin", help=f"Codex CLI path (default PATH or {CODEX_BIN_ENV})")
    add_selection_arguments(models)
    workstream = sub.add_parser("workstream", help="manage persistent workstream budgets")
    workstream_sub = workstream.add_subparsers(dest="workstream_command", required=True)
    reset_parser = workstream_sub.add_parser("reset", help="explicitly reset both tier budgets for one workstream")
    reset_parser.add_argument("--repo", required=True, help="repository whose budget should be reset")
    reset_parser.add_argument("--workstream", required=True, help="stable workstream name")
    reset_parser.add_argument("--state-dir")
    reset_parser.add_argument("--actor", default=os.environ.get("USER") or "cli")
    check = sub.add_parser("preflight", help="check the installed Codex CLI, then discover the "
                                             "loaded model and its limits; does not qualify "
                                             "native execution")
    check.add_argument("--settings", default=str(OMLX_SETTINGS))
    check.add_argument("--codex-bin", help=f"Codex CLI path (default PATH or {CODEX_BIN_ENV}, "
                                           f"legacy {LEGACY_CODEX_BIN_ENV})")
    add_selection_arguments(check)

    perms = sub.add_parser("permissions", help="show or change the user-owned permissions list "
                                               "that decides what workers may read")
    perms_sub = perms.add_subparsers(dest="permissions_command", required=True)
    for name, help_text in (
            ("show", "print the effective permissions list and whether the Seatbelt wrapper can "
                     "run (creates the default file when there is none)"),
            ("deny-read", "hide PATH from workers: add it to the deny_read list"),
            ("allow-read", "stop hiding PATH from workers: remove it from the deny_read list "
                           "(default entries too, the file is yours)"),
            ("deny-write", "stop workers writing PATH: add it to the deny_write list"),
            ("allow-write", "stop denying writes to PATH: remove it from the deny_write list "
                            "(default entries too, the file is yours)")):
        item = perms_sub.add_parser(name, help=help_text)
        if name != "show":
            item.add_argument("path", metavar="PATH")
        item.add_argument("--state-dir",
                          help="state root holding permissions.json (env %s, legacy %s; "
                               "default %s)" % (STATE_ENV, LEGACY_STATE_ENV, DEFAULT_STATE_DIR))
    return parser


def summary_lines(report: Dict[str, Any]) -> List[str]:
    model = (report.get("model_profile") or {}).get("model") or {}
    if report.get("api_model"):
        model_line = (f"model: {model.get('canonical_id')} -> {report.get('api_model')} @ "
                      f"{report.get('endpoint')} (context {report.get('context_window')})")
    else:
        model_line = "model: not discovered"
    lines = [
        f"status: {report['status']} (exit {report.get('exit_code')})",
        model_line,
        f"worktree: {report.get('worktree') or 'not created'}",
        f"report: {report.get('report_dir') or report.get('report_path') or 'not written'}",
        f"changed paths: {len(report.get('changed_paths', []))}",
    ]
    for violation in report.get("violations", [])[:10]:
        lines.append(f"violation: {violation['path']} ({violation['reason']})")
    for test in report.get("tests", []):
        lines.append(f"test {json.dumps(test['command'])}: {test['status']}")
    record = report.get("permissions") or {}
    if record.get("deny_list_applied"):
        lines.append(f"permissions: deny list applied, {len(record.get('denials') or [])} "
                     "denial(s)")
    else:
        lines.append(f"permissions: deny list NOT applied ({record.get('reason') or 'no reason recorded'})")
    if report.get("primary_changed_during_job"):
        lines.append("primary checkout: changed during job, not attributed"
                     if record.get("deny_list_applied") else
                     "primary checkout: changed during job (deny list not applied)")
    sandbox = report.get("sandbox") or {}
    if sandbox.get("backend") == FULL_HOST_BACKEND:
        lines.append(f"backend: {FULL_HOST_BACKEND} confined=no network=enabled "
                     f"simulator {sandbox.get('apple_simulator_udid') or 'not selected'}")
    lease = report.get("lease") or {}
    if lease.get("capacity", 1) > 1:
        resource = f", resource {lease['resource']}" if lease.get("resource") else ""
        lines.append(f"lease: slot {lease.get('slot')}/{lease.get('capacity')} with "
                     f"{lease.get('legacy_lock')} legacy lock, waited "
                     f"{lease.get('wait_seconds')}s{resource}")
    timing = report.get("timing") or {}
    usage = report.get("usage") or {}
    lines.append(f"actual seconds: job={timing.get('job_seconds')} worker={timing.get('worker_seconds')} verification={timing.get('verification_seconds')}")
    lines.append(f"usage ({usage.get('availability', 'unavailable')}, source={usage.get('source')}): input={usage.get('input_tokens')} cached={usage.get('cached_input_tokens')} output={usage.get('output_tokens')}")
    if report.get("failure_reason"):
        lines.append(f"reason: {report['failure_reason']}")
    lines.append("note: post-run audit detects out-of-scope edits; it does not prevent them")
    return lines


def planner_json(report: Dict[str, Any]) -> Dict[str, Any]:
    """Stable, compact ``run --json`` contract for the planner (spec §11)."""
    report_dir = report.get("report_dir")
    return {
        "job_id": report.get("job_id"),
        "kind": report.get("kind", "write"),
        "tier": report.get("tier", "local"),
        "model": report.get("api_model"),
        "status": report.get("status"),
        "classification": report.get("classification"),
        "answer": report.get("answer"),
        "patch_path": report.get("patch_path"),
        "screenshots": report.get("screenshots", []),
        "tests": report.get("tests", []),
        "tokens": report.get("usage"),
        "durations": report.get("timing", {}),
        "report_path": str(Path(report_dir) / "report.json") if report_dir else None,
    }


def status_snapshot(item):
    """Read process liveness without treating missing output as a stalled model."""
    item = dict(item)
    item.setdefault("phase", item.get("status"))
    item.setdefault("last_event_at", None)
    if not item.get("active"):
        return item
    pid = item.get("launcher_pid")
    item["liveness_source"] = "launcher_pid" if isinstance(pid, int) and pid > 0 else "unavailable"
    if item["liveness_source"] == "unavailable":
        return item
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        item.update(active=False, status="interrupted", phase="interrupted",
                    failure_reason="launcher process no longer exists; final usage may be incomplete")
        usage = dict(item.get("usage") or {})
        if usage:
            usage["complete"] = False
            if usage.get("availability") == "complete":
                usage["availability"] = "partial"
            item["usage"] = usage
        return item
    except PermissionError:
        pass  # The process exists, although it belongs to another security context.
    started = item.get("job_started_at") or item.get("started_at")
    if started:
        with contextlib.suppress(ValueError, TypeError):
            item["elapsed_seconds"] = round(max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()), 3)
    return item


def selection_from_args(args: argparse.Namespace) -> Tuple[Optional[str], Optional[int]]:
    """The validated ``(--model, --context-cap)`` of one invocation; ``None`` means unset."""
    model = getattr(args, "model", None)
    if model is not None and not str(model).strip():
        raise ContractError("--model must not be empty")
    cap = getattr(args, "context_cap", None)
    return model, None if cap is None else validate_context_cap(cap)


def refusal_payload(error: LauncherError) -> Dict[str, Any]:
    """What the CLI prints for a refused invocation; a discovery refusal also carries its code
    and the model names the caller could choose instead."""
    payload: Dict[str, Any] = {"status": error.status, "failure_reason": str(error)}
    if isinstance(error, CodexIncompatible):
        payload["codex_compat"] = error.compat
    if isinstance(error, PreflightError) and error.code:
        payload["discovery_error"] = {"code": error.code, "message": str(error),
                                      "candidates": list(error.candidates)}
    return payload


def run_permissions_command(args: argparse.Namespace) -> int:
    """``permissions show|deny-read|allow-read|deny-write|allow-write``: print the effective list
    after any change."""
    state = resolve_state_dir(args.state_dir)
    changers = {
        "deny-read": permissions.add_deny_read, "allow-read": permissions.remove_deny_read,
        "deny-write": permissions.add_deny_write, "allow-write": permissions.remove_deny_write,
    }
    change = changers.get(args.permissions_command)
    current = change(state, args.path) if change else permissions.load_permissions(state)
    print(json.dumps(permissions.describe(current), indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None, *,
         omlx_fetch: omlx_discovery.HttpFetch = omlx_discovery.http_fetch,
         codex_run: Callable[..., Any] = subprocess.run) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "status":
            state = resolve_state_dir(args.state_dir)
            if args.job_id and not re.fullmatch(r"[A-Za-z0-9._-]+", args.job_id):
                raise ContractError("invalid job ID")
            paths = [state / "reports" / args.job_id / "progress.json"] if args.job_id else sorted((state / "reports").glob("*/progress.json"))
            result = []
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    continue
                item = json.loads(path.read_text())
                result.append(status_snapshot(item))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "workstream":
            repo = Path(os.path.expanduser(args.repo)).absolute()
            if not repo.is_dir():
                raise ContractError("workstream reset repo must be an existing directory")
            result = policy.reset(resolve_state_dir(args.state_dir), repo_identity(repo),
                                  args.workstream, actor=args.actor)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "models":
            if args.cloud:
                try:
                    data = [model.__dict__ for model in codex_catalog.list_cloud_models(
                        resolve_codex_bin(args.codex_bin), run=codex_run)]
                except RuntimeError as error:
                    raise PreflightError(str(error)) from error
            else:
                requested_model, context_cap = selection_from_args(args)
                data = run_preflight(Path(os.path.expanduser(args.settings)), requested_model,
                                     context_cap, omlx_fetch)
            if args.json:
                print(json.dumps(data, indent=2))
            elif args.cloud:
                for model in data:
                    print("%s: %s (%s tokens)" %
                          (model["slug"], model["description"], model["context_window"]))
            else:
                print(json.dumps(data, indent=2))
            return 0
        if args.command == "permissions":
            return run_permissions_command(args)
        requested_model, context_cap = selection_from_args(args)
        if args.command == "preflight":
            compat = require_codex_compat(resolve_codex_bin(args.codex_bin), run=codex_run)
            evidence = run_preflight(Path(os.path.expanduser(args.settings)), requested_model,
                                     context_cap, omlx_fetch)
            evidence["codex_compat"] = compat
            print(json.dumps(evidence, indent=2))
            return 0

        if args.load and requested_model is None:
            raise ContractError("--load requires --model")
        task = Task.from_payload(json.loads(Path(args.task_file).read_text(encoding="utf-8")))
        udid = args.apple_simulator_udid
        if udid is not None:
            try:
                udid = apple.validate_simulator_udid(udid)
            except apple.ApplePolicyError as error:
                raise ContractError(str(error)) from error
        maximum = parse_max_workers(args.max_workers)
        options = JobOptions(
            task=task,
            state_dir=resolve_state_dir(args.state_dir),
            codex_bin=resolve_codex_bin(args.codex_bin),
            lock_timeout=args.lock_timeout,
            settings_path=Path(os.path.expanduser(args.settings)),
            requested_model=requested_model,
            context_cap=context_cap,
            load_model=args.load,
            omlx_fetch=omlx_fetch,
            verify=not args.no_verify,
            apple_simulator_udid=udid,
            maximum=maximum,
            native_preflight_plan=args.native_preflight_plan,
            preflight_only=args.preflight_only,
            workstream=args.workstream or task.workstream,
            job_kind=args.job_kind,
            tool_config=args.tool_config,
            escalates=args.escalates,
            codex_run=codex_run,
        )
        previous_handlers = {}
        def interrupt_handler(signum, _frame):
            raise Interrupted(f"launcher interrupted by signal {signal.Signals(signum).name}")
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, interrupt_handler)
        try:
            report = run_job(options)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    except (LauncherError, ContractError) as error:
        print(json.dumps(refusal_payload(error), indent=2))
        return EXIT_CODES.get(error.status, 4)
    except permissions.PermissionsError as error:
        print(json.dumps({"status": "preflight_failed", "failure_reason": str(error)}, indent=2))
        return EXIT_CODES["preflight_failed"]
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "contract_invalid",
                          "failure_reason": f"cannot read task contract: {error}"}, indent=2))
        return EXIT_CODES["contract_invalid"]

    if args.json:
        print(json.dumps(planner_json(report), indent=2))
    else:
        print("\n".join(summary_lines(report)))
    return int(report.get("exit_code", 4))


if __name__ == "__main__":
    sys.exit(main())
