#!/usr/bin/env python3
"""Local-first implementation worker: drive one Codex CLI worker against a local oMLX model.

Standard library only. Design rules:

* Endpoint is the literal loopback URL below; proxies and redirects are refused.
* The exact canonical model must map to the expected API alias, the server must have
  model fallback disabled, and the alias must appear in the served model list.
* The oMLX key is loaded into the child environment only; it is excluded from the
  model's shell environment via shell_environment_policy and redacted from all logs.
* Every job runs in its own detached ``git worktree``. The primary checkout is never
  mutated. Failed worktrees are preserved, never auto-merged, never committed.
* One job runs at a time by default. ``--max-workers`` 2-4 widens that to a bounded pool of
  exclusive slot leases in the same shared state directory, always mutually exclusive with a
  default job and always refused when owned paths of an active same-repository contract
  overlap. There is no fork scheduler: the planner starts every job itself.
* Post-run audit *detects* out-of-scope edits; it does not prevent them. Execution is
  full-host: no sandbox confines the worker, so it can read anything the launcher can.
* Independent verification runs the contract commands directly on the host. Every result is
  logged truthfully with ``sandboxed: false``; a command that could not be run is reported
  blocked with the reason it was not run.

All guarantees apply only to jobs launched through this launcher.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import apple_verification as apple
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
HOST = "127.0.0.1"
PORT = 8000
API_PREFIX = "/v1"
ENDPOINT = "http://127.0.0.1:8000/v1"
CANONICAL_MODEL = "Qwen3.8-Flash-Next-UD-Q4_K_XL"
EXPECTED_ALIAS = "qwen-3.8-nf-splash"
PROVIDER_ID = "local-omlx"
API_KEY_ENV = "LOCAL_OMLX_API_KEY"

OMLX_DIR = Path("~/.omlx")
OMLX_SETTINGS = OMLX_DIR / "settings.json"
OMLX_MODEL_SETTINGS = OMLX_DIR / "model_settings.json"

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
MAX_TIMEOUT_SECONDS = 3600
ALLOWED_ATTEMPTS = (1, 2)
LOCK_TIMEOUT_SECONDS = 30.0
PROBE_MARKER = "__LOCAL_WORKER_SANDBOX_OK__"
PATCH_REPORT_BYTES = 262144
HTTP_TIMEOUT_SECONDS = 15
TERMINATE_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 10.0
MIN_ATTEMPT_BUDGET_SECONDS = 5.0

# Default execution backend: the worker and every verification command run with the
# launcher's own host permissions and network access. Nothing confines them, so the report
# says so instead of claiming a confinement that is not applied.
FULL_HOST_BACKEND = "full-host-access"
# There is no profile and no wrapper: the command is the contract argv, run directly.
FULL_HOST_PROFILE = "none"
FULL_HOST_PROBE_ARGV = ["/bin/echo", PROBE_MARKER]
FULL_HOST_CONFINEMENT = "none (full host: launcher permissions, network enabled)"
# Legacy Codex workspace-write backend: kept usable for a caller that explicitly asks for
# it. The installed codex sandbox refuses to start without ``-P <profile>``, so the profile
# and its exact overrides stay pinned here.
SANDBOX_PROFILE = "local-worker-verify"
PROBE_TIMEOUT_SECONDS = 60.0
# Reported with every job so a reviewer always knows what confined the verification run.
WORKSPACE_SANDBOX_BACKEND = "codex-workspace-write"
MODEL_CONTEXT_WINDOW = 65536

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
# Literal ``-c key=value`` overrides that must be present in the worker argv. The worker
# runs with --ignore-user-config, so these pins are the whole policy: no approval
# prompts, no web search, no provider retries (a retry could mask a routing problem), a
# bounded context window, and the local key excluded from model-generated shell commands.
CONFIG_PINS = {
    "model_provider": f'"{PROVIDER_ID}"',
    f"model_providers.{PROVIDER_ID}.base_url": f'"{ENDPOINT}"',
    f"model_providers.{PROVIDER_ID}.request_max_retries": "0",
    f"model_providers.{PROVIDER_ID}.stream_max_retries": "0",
    "approval_policy": '"never"',
    "web_search": '"disabled"',
    "sandbox_mode": '"danger-full-access"',
    "model_context_window": str(MODEL_CONTEXT_WINDOW),
    "shell_environment_policy.inherit": '"core"',
}

PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "GATEWAY_INTERFACE",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "CURL_HOME",
)
BASE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR",
    "TMP", "TEMP", "TZ", "CODEX_HOME", "XPC_SERVICE_NAME",
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

FEATURE_FLAGS = {
    "features.apps": False,
    "features.plugins": False,
    "features.remote_plugin": False,
    "features.multi_agent": False,
    "features.skip_host_skill_discovery": True,
}
# Non-negotiable CLI flags of the worker invocation. The worker never loads the user's config
# or rules, so these flags plus the ``-c`` pins are the entire policy: full-host access,
# machine-readable events and no interactive approval can never be inherited by accident.
REQUIRED_WORKER_FLAGS = ("--ignore-user-config", "--ignore-rules", "--json")
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
}

LIMITATIONS = (
    "The allowed-path audit runs after the worker finishes: it detects out-of-scope "
    "edits, it cannot prevent them while the worker runs.",
    "Execution is full-host: the worker and every verification command run with the "
    "launcher's own permissions and network access, with no sandbox confinement. The "
    "post-run audit and the task contract are the only scope controls, so treat any file "
    "the host can read or write as reachable by a job.",
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


class ContractError(LauncherError):
    status = "contract_invalid"


class PreflightError(LauncherError):
    status = "preflight_failed"


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


# --------------------------------------------------------------------------------------
# Task contract
# --------------------------------------------------------------------------------------
CONTRACT_FIELDS = (
    "task_id", "repo_path", "base_ref", "goal", "acceptance_criteria", "allowed_paths",
    "test_commands", "timeout_seconds", "max_attempts",
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REF_BAD_CHARS = re.compile(r"[\s~^:?*\[\]\\]|\.\.|@\{")


class _Field:
    def __init__(self, name: str) -> None:
        self.name = name

    def fail(self, message: str) -> None:
        raise ContractError(f"{self.name}: {message}")

    def string(self, value: Any, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            self.fail(f"must be a string, got {type(value).__name__}")
        if not allow_empty and not value.strip():
            self.fail("must be a non-empty string")
        if "\x00" in value:
            self.fail("must not contain NUL")
        return value

    def integer(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(f"must be an integer, got {value!r}")
        return int(value)

    def string_list(self, value: Any, *, allow_empty: bool = False) -> List[str]:
        if not isinstance(value, list):
            self.fail(f"must be an array of strings, got {type(value).__name__}")
        if not allow_empty and not value:
            self.fail("must not be empty")
        return [self.string(item) for item in value]


def normalize_relative_path(raw: str, field_name: str) -> str:
    """Return a repo-relative path, refusing absolute, traversing and .git paths."""
    _Field(field_name).string(raw)
    if raw.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        raise ContractError(f"{field_name}: must be relative to the repo root, got {raw!r}")
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        raise ContractError(f"{field_name}: must not be empty or '.'")
    if ".." in parts:
        raise ContractError(f"{field_name}: parent traversal is not allowed ({raw!r})")
    if parts[0] == ".git" or ".git" in parts:
        raise ContractError(f"{field_name}: repository metadata paths are not allowed ({raw!r})")
    return "/".join(parts)


@dataclass
class Task:
    task_id: str
    repo_path: Path
    base_ref: str
    goal: str
    acceptance_criteria: List[str]
    allowed_paths: List[str]
    test_commands: List[List[str]]
    timeout_seconds: int
    max_attempts: int

    @classmethod
    def from_payload(cls, payload: Any) -> "Task":
        if not isinstance(payload, dict):
            raise ContractError(f"contract must be a JSON object, got {type(payload).__name__}")
        unknown = sorted(set(payload) - set(CONTRACT_FIELDS))
        if unknown:
            raise ContractError(f"unknown contract field(s): {', '.join(unknown)}")
        missing = [name for name in CONTRACT_FIELDS if name not in payload]
        if missing:
            raise ContractError(f"missing contract field(s): {', '.join(missing)}")

        task_id = _Field("task_id").string(payload["task_id"])
        if not TASK_ID_RE.match(task_id):
            raise ContractError(
                "task_id: must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (no path separators)"
            )

        repo_raw = _Field("repo_path").string(payload["repo_path"])
        repo_path = Path(repo_raw)
        if not repo_path.is_absolute():
            raise ContractError(f"repo_path: must be absolute, got {repo_raw!r}")
        if not repo_path.is_dir():
            raise ContractError(f"repo_path: no such directory: {repo_raw}")

        base_ref = _Field("base_ref").string(payload["base_ref"])
        if base_ref.startswith("-"):
            raise ContractError("base_ref: must not start with '-'")
        if REF_BAD_CHARS.search(base_ref):
            raise ContractError(f"base_ref: invalid git revision specifier {base_ref!r}")

        goal = _Field("goal").string(payload["goal"])
        criteria = _Field("acceptance_criteria").string_list(payload["acceptance_criteria"])

        allowed_raw = _Field("allowed_paths").string_list(payload["allowed_paths"])
        allowed_paths: List[str] = []
        for raw in allowed_raw:
            normalized = normalize_relative_path(raw, "allowed_paths[]")
            if normalized not in allowed_paths:
                allowed_paths.append(normalized)
        if not allowed_paths:
            raise ContractError("allowed_paths: at least one path is required")

        raw_commands = payload["test_commands"]
        if not isinstance(raw_commands, list):
            raise ContractError(
                f"test_commands: must be an array of argv arrays, got {type(raw_commands).__name__}"
            )
        if not raw_commands:
            raise ContractError("test_commands: must contain at least one argv array")
        test_commands: List[List[str]] = []
        for index, argv in enumerate(raw_commands):
            if isinstance(argv, str) or not isinstance(argv, list):
                raise ContractError(
                    f"test_commands[{index}]: must be an argv array (no shell strings)"
                )
            if not argv:
                raise ContractError(f"test_commands[{index}]: must not be empty")
            cleaned = [str(_Field(f"test_commands[{index}][]").string(part)) for part in argv]
            test_commands.append(cleaned)

        timeout_seconds = _Field("timeout_seconds").integer(payload["timeout_seconds"])
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ContractError(
                f"timeout_seconds: must be between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS}, got {timeout_seconds}"
            )

        max_attempts = _Field("max_attempts").integer(payload["max_attempts"])
        if max_attempts not in ALLOWED_ATTEMPTS:
            raise ContractError(f"max_attempts: must be one of {list(ALLOWED_ATTEMPTS)}, got {max_attempts}")

        return cls(
            task_id=task_id, repo_path=repo_path, base_ref=base_ref, goal=goal,
            acceptance_criteria=criteria, allowed_paths=allowed_paths,
            test_commands=test_commands, timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repo_path": str(self.repo_path),
            "base_ref": self.base_ref,
            "goal": self.goal,
            "acceptance_criteria": self.acceptance_criteria,
            "allowed_paths": self.allowed_paths,
            "test_commands": self.test_commands,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
        }


# --------------------------------------------------------------------------------------
# Loopback endpoint + local provider validation
# --------------------------------------------------------------------------------------
ENDPOINT_RE = re.compile(r"^http://127\.0\.0\.1:8000/v1/?$")


def validate_endpoint(endpoint: Any) -> str:
    """Accept only the literal loopback endpoint; anything else is a hard refusal."""
    if not isinstance(endpoint, str) or not ENDPOINT_RE.match(endpoint):
        raise PreflightError(
            f"endpoint must be the literal loopback URL {ENDPOINT}, got {endpoint!r}"
        )
    return ENDPOINT


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects are refused: a 3xx must surface as an error, never be followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """Opener with environment proxies disabled and redirects refused."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseRedirects())


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


def fetch_model_ids(endpoint: str, api_key: str, opener: Any = None) -> List[str]:
    """GET <endpoint>/models with no proxy and no redirect following."""
    validate_endpoint(endpoint)
    request = urllib.request.Request(
        f"{endpoint}/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with (opener or build_opener()).open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise PreflightError(
            f"{endpoint}/models returned HTTP {error.code}; authentication or model "
            "configuration must be fixed before any worker launch"
        ) from error
    except Exception as error:  # URLError, socket timeout, decoded errors
        raise PreflightError(
            f"cannot query {endpoint}/models ({error.__class__.__name__}); is oMLX running "
            "on the loopback endpoint?"
        ) from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise PreflightError(f"{endpoint}/models did not return JSON") from error
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise PreflightError(f"{endpoint}/models response has no 'data' list")
    return [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]


def _read_json_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        loaded = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PreflightError(f"{label} not found at {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PreflightError(f"cannot read {label} at {path}: {error.__class__.__name__}") from error
    if not isinstance(loaded, dict):
        raise PreflightError(f"{label} must contain a JSON object")
    return loaded


def read_api_key(settings_path: Path = OMLX_SETTINGS) -> str:
    settings = _read_json_object(settings_path, "oMLX settings")
    auth = settings.get("auth")
    key = auth.get("api_key") if isinstance(auth, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise PreflightError(f"{settings_path} has no auth.api_key; refusing to launch")
    host = settings.get("server") if isinstance(settings.get("server"), dict) else {}
    declared_host, declared_port = host.get("host"), host.get("port")
    if declared_host not in (None, "", HOST) or declared_port not in (None, PORT):
        raise PreflightError(
            f"oMLX listens on {declared_host!r}:{declared_port!r}, which does not match the "
            f"pinned loopback endpoint {ENDPOINT}"
        )
    return key.strip()


def read_model_fallback(settings_path: Path = OMLX_SETTINGS) -> bool:
    settings = _read_json_object(settings_path, "oMLX settings")
    model = settings.get("model") if isinstance(settings.get("model"), dict) else {}
    fallback = model.get("model_fallback")
    if fallback is not False:
        raise PreflightError(
            f"server setting model.model_fallback must be false, got {fallback!r}; "
            "fallback could silently replace the requested local model"
        )
    return False


def read_model_alias(
    model_settings_path: Path = OMLX_MODEL_SETTINGS,
    canonical_model: str = CANONICAL_MODEL,
    expected_alias: str = EXPECTED_ALIAS,
) -> str:
    data = _read_json_object(model_settings_path, "oMLX model settings")
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    entry = models.get(canonical_model)
    if not isinstance(entry, dict):
        raise PreflightError(
            f"{model_settings_path} has no saved settings for canonical model {canonical_model!r}"
        )
    alias = entry.get("model_alias")
    if not isinstance(alias, str) or not alias.strip():
        raise PreflightError(
            f"canonical model {canonical_model!r} has no model_alias; refusing to guess an API model"
        )
    if alias != expected_alias:
        raise PreflightError(
            f"canonical model {canonical_model!r} maps to {alias!r}, not the expected "
            f"alias {expected_alias!r}; refusing to switch models"
        )
    return alias


def run_preflight(
    *,
    settings_path: Path = OMLX_SETTINGS,
    model_settings_path: Path = OMLX_MODEL_SETTINGS,
    endpoint: str = ENDPOINT,
    canonical_model: str = CANONICAL_MODEL,
    expected_alias: str = EXPECTED_ALIAS,
    list_models: Callable[[str, str], List[str]] = fetch_model_ids,
) -> Dict[str, Any]:
    """Validate server settings, alias mapping and model availability. Never returns the key."""
    endpoint = validate_endpoint(endpoint)
    api_key = read_api_key(settings_path)
    read_model_fallback(settings_path)
    alias = read_model_alias(model_settings_path, canonical_model, expected_alias)
    try:
        served = list_models(endpoint, api_key)
    finally:
        api_key = ""
    if alias not in served:
        raise PreflightError(
            f"API model list at {endpoint}/models does not contain {alias!r}; the exact "
            "model is not loaded and fallback is disabled, so no launch will be attempted"
        )
    return {
        "endpoint": endpoint,
        "canonical_model": canonical_model,
        "api_model": alias,
        "model_fallback": False,
        "api_key": "present (value withheld)",
        "models_endpoint": f"{endpoint}/models",
        "served_models": sorted(served),
        "proxies": "disabled",
        "redirects": "refused",
    }


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


def status_entries(repo: Path, *, ignored: bool = False,
                   env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """``git status`` entries without touching the index (--no-optional-locks)."""
    argv = ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
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


def _digest_path(path: Path) -> str:
    """Content fingerprint for one path; comparable before/after without holding data."""
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(str(path))
        if path.is_dir():
            return "directory"
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        return f"unreadable:{error.__class__.__name__}"


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
                   scratch_dir: Optional[Path] = None) -> Audit:
    """Audit everything that differs from the *original* base commit.

    The comparison target is the commit the job started from, never the moving HEAD, so
    committed, staged, unstaged, deleted, renamed (both sides) and untracked work are all
    visible. Staging happens in a throwaway ``GIT_INDEX_FILE``: the worktree index and
    HEAD are never mutated, and a worker's commits are reported instead of reset.
    """
    audit = Audit(base_commit=base_commit)
    audit.head_commit = rev_parse(worktree, "HEAD")
    if audit.head_commit != base_commit:
        audit.head_changed = True
        audit.violations.append({
            "path": ".",
            "reason": "worker moved HEAD (git commit is forbidden); commits are preserved "
                      f"for review, not hidden (base {base_commit[:12]} -> "
                      f"head {audit.head_commit[:12]})",
        })

    scratch = audit_scratch_dir(worktree, scratch_dir)
    index_path = scratch / f"audit-index-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    audit_env = {"GIT_INDEX_FILE": str(index_path)}
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

    try:
        git(worktree, "read-tree", base_commit, env=audit_env)
        git(worktree, "add", "-A", env=audit_env)
        for status, path in status_entries(worktree):
            record(status, path)
        name_status = git(worktree, "--no-optional-locks", "diff", "--cached", "--name-status",
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

        diff = git(worktree, "--no-optional-locks", "diff", "--cached", "--binary",
                   *DIFF_ARGS, base_commit, env=audit_env)
        audit.patch = redact(diff.stdout.decode("utf-8", "replace"), secrets).encode("utf-8")
        stat = git(worktree, "--no-optional-locks", "diff", "--cached", "--stat",
                   *DIFF_ARGS, base_commit, env=audit_env)
        audit.diffstat = redact(stat.stdout.decode("utf-8", "replace"), secrets).strip()

        changed = set(audit.changed_paths)
        ignored = sorted(
            {path.rstrip("/") for status, path in status_entries(worktree, ignored=True)
             if status == "!!"}
        )
        audit.ignored_paths = [path for path in ignored if path not in changed]
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
                timeout: float, stdin_data: Optional[bytes] = None) -> Dict[str, Any]:
    """Run argv in its own process group and kill the whole group on deadline."""
    observer = _RUN_OBSERVER.get()
    if observer is not None and stdin_data is not None:
        return observation.run_streaming(argv, cwd=cwd, env=env, timeout=timeout,
                                         stdin_data=stdin_data, observer=observer,
                                         terminate_group=_terminate_group)
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv), cwd=str(cwd) if cwd else None, env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    timed_out = False
    stdout = stderr = b""
    termination: Dict[str, Any] = {"terminated": False, "force_killed": False, "survivors": False}
    try:
        stdout, stderr = process.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        termination = _terminate_group(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            stdout, stderr = process.communicate(timeout=5)
    except BaseException:
        # An interrupt or an internal failure must not leave the worker running: the streaming
        # runner kills the group before propagating, and this path has to do the same.
        termination = _terminate_group(process)
        raise
    return {
        "argv": list(argv), "returncode": process.returncode, "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 2), "stdout": stdout or b"",
        "stderr": stderr or b"", "termination": termination,
    }


# --------------------------------------------------------------------------------------
# Worker invocation
# --------------------------------------------------------------------------------------
def config_overrides(alias: str) -> List[str]:
    """Explicit local provider/model overrides; the user's config is never loaded."""
    provider = f"model_providers.{PROVIDER_ID}"
    settings = [
        ("model", f'"{alias}"'),
        ("model_provider", f'"{PROVIDER_ID}"'),
        (f"{provider}.name", '"Local oMLX (loopback only)"'),
        (f"{provider}.base_url", f'"{ENDPOINT}"'),
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
        ("sandbox_mode", '"danger-full-access"'),
        ("model_context_window", str(MODEL_CONTEXT_WINDOW)),
    ]
    for key, value in sorted(FEATURE_FLAGS.items()):
        settings.append((key, "true" if value else "false"))
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


def expected_config_pins(alias: str = EXPECTED_ALIAS) -> Dict[str, str]:
    """The full pinned ``-c`` policy, derived from the single place that builds it."""
    return config_overrides_map(config_overrides(alias))


def missing_config_pins(argv: Sequence[str]) -> List[str]:
    """Self-check before launching: any drift from the pinned local policy is a refusal.

    ``--ignore-user-config`` means the argv *is* the whole policy, so routing flags and every
    pinned override are checked together: a missing pin, a changed value, a conflicting
    duplicate override or a second model/sandbox flag all refuse the launch instead of letting
    the last occurrence quietly win.
    """
    pairs = config_override_pairs(argv)
    overrides = dict(pairs)
    problems: List[str] = []
    pinned = expected_config_pins()
    for key, expected in CONFIG_PINS.items():
        # The documented floor stays required on its own: deleting a pin from
        # config_overrides() must not silently delete the requirement for it.
        pinned.setdefault(key, expected)
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
        if key.endswith(".base_url") and value != f'"{ENDPOINT}"':
            problems.append(f"{key} must stay on the loopback endpoint {ENDPOINT}, got {value}")
    for flag in REQUIRED_WORKER_FLAGS:
        if flag not in argv:
            problems.append(f"{flag} is missing")
    problems.extend(_single_valued_flag(argv, SANDBOX_FLAGS, REQUIRED_SANDBOX_MODE))
    problems.extend(_single_valued_flag(argv, MODEL_FLAGS, EXPECTED_ALIAS))
    inherit = overrides.get("shell_environment_policy.inherit")
    if inherit not in ('"none"', '"core"'):
        problems.append('shell_environment_policy.inherit must be "none" or "core", '
                        f"got {inherit!r}")
    if inherit == '"core"' and API_KEY_ENV not in shell_env_excludes(
            overrides.get("shell_environment_policy.exclude")):
        problems.append(f"shell_environment_policy.exclude must exclude {API_KEY_ENV}")
    return problems


def worker_argv(codex_bin: str, worktree: Path, alias: str, last_message: Path,
                tool_overrides: Sequence[str] = (),
                shell_excludes: Sequence[str] = ()) -> List[str]:
    argv = [
        codex_bin, "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "-C", str(worktree),
        "-s", "danger-full-access",
        "--json",
        "-o", str(last_message),
        "-m", alias,
        *config_overrides(alias),
    ]
    argv = with_shell_env_excludes(argv, shell_excludes)
    argv.extend(str(item) for item in tool_overrides)
    argv.extend(["--", "-"])
    return argv


def worker_env(api_key: str) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Minimal child environment: loopback key present, proxies and cloud credentials gone."""
    cleaned, stripped = strip_proxy_env(dict(os.environ))
    cloud = cloud_credential_names(cleaned)
    env = {key: cleaned[key] for key in BASE_ENV_KEYS if key in cleaned}
    env.setdefault("TERM", "dumb")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env[API_KEY_ENV] = api_key
    return env, stripped, cloud


PROMPT_RULES = (
    "You are a local implementation worker. Work only in the current working directory.",
    "Create or modify files ONLY under the allowed paths listed below.",
    "Do not run git commit, git merge, git push, or git worktree commands.",
    "Do not read or print credentials, tokens, or environment secrets.",
    "Run the verification commands listed below and keep iterating until they pass.",
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
                          reserve: Optional[float], attempt_cap: Optional[float]) -> List[str]:
    """What the worker is actually allowed to spend on *this* attempt, in its own words.

    The total job budget alone is not actionable: the worker is killed much earlier, at
    ``min(remaining - verification reserve, per-attempt cap)``.
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
    if attempt_cap is not None:
        parts.append(f"per-attempt cap {float(attempt_cap):.1f}s")
    line = (f"- This attempt: the worker and its whole process group are killed at "
            f"{effective:.1f}s.")
    if parts:
        line += f" That effective cutoff is min({', '.join(parts)})."
    return [line, "- Budget each step against that cutoff, not against the total above."]


def _budget_section(task: Task, deadline: Optional[Deadline],
                    attempt_timeout: Optional[float] = None,
                    reserve: Optional[float] = None,
                    attempt_cap: Optional[float] = None) -> List[str]:
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
        "- Budget the steps that are still ahead, including the verification commands above, "
        "to fit what is left.",
    ]
    if deadline is None:
        lines.append("- Remaining now: not measured by this render; the launcher tracks the "
                     "shared budget itself.")
        lines.extend(_attempt_budget_lines(deadline, attempt_timeout, reserve, attempt_cap))
        return lines
    left = max(0.0, deadline.remaining())
    ends_at = (datetime.now(timezone.utc) + timedelta(seconds=left)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"- Remaining now: {left:.1f}s of that total (hard deadline {ends_at} UTC).")
    lines.extend(_attempt_budget_lines(deadline, attempt_timeout, reserve, attempt_cap))
    return lines


def build_prompt(task: Task, worktree: Path, feedback: Optional[str] = None,
                 apple_udid: Optional[str] = None,
                 deadline: Optional[Deadline] = None,
                 attempt_timeout: Optional[float] = None,
                 reserve: Optional[float] = None,
                 attempt_cap: Optional[float] = None) -> str:
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
        "Verification commands (argv arrays, run from the worktree root):",
        *[f"- {json.dumps(argv)}" for argv in task.test_commands],
        *_budget_section(task, deadline, attempt_timeout=attempt_timeout, reserve=reserve,
                         attempt_cap=attempt_cap),
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


def _walk_key(node: Any, key: str) -> List[Any]:
    found: List[Any] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            found.extend(_walk_key(value, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_key(item, key))
    return found


UNSUPPORTED_RE = re.compile(r"unsupported (?:tool|function)|tool .{0,40}not (?:supported|available)", re.I)
# A raw (non-JSON) stream line is only a diagnostic when it reads like one: plain progress text
# that happens to quote the phrase must stay silent, while a CLI error line must fail closed.
DIAGNOSTIC_RE = re.compile(r"\b(?:error|errors|fatal|panic|exception|traceback|failed|failure|"
                           r"warning|refused|rejected|denied)\b", re.I)
# Event kinds the CLI itself labels as failures, and the field names that carry a failure status.
ERROR_EVENT_TYPES = ("error", "tool_error", "tool_call_error", "function_call_error")
FAILURE_STATUSES = ("failed", "failure", "error")
FAILURE_STATUS_KEYS = ("status", "exit_status", "state", "outcome")
# Only these fields of a failure node are diagnostics. Command text, tool output of a merely
# non-zero exit, reasoning and chat text are data the worker quotes, never tool-failure evidence.
DIAGNOSTIC_KEYS = ("error", "message", "detail", "details", "reason", "msg")
PAYLOAD_KEYS = ("output", "content", "result", "text")


def _walk_dicts(node: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk_dicts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_dicts(item))
    return found


def _walk_strings(node: Any) -> List[str]:
    found: List[str] = []
    if isinstance(node, str):
        found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_walk_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_strings(item))
    return found


def _present(value: Any) -> bool:
    return value not in (None, False, "", {}, [], 0)


def _failure_texts(event: Any) -> List[str]:
    """Diagnostic strings of real failure events; content streams are never searched."""
    texts: List[str] = []
    for node in _walk_dicts(event):
        kind = node.get("type")
        kind = kind.lower() if isinstance(kind, str) else ""
        declared = (kind in ERROR_EVENT_TYPES or _present(node.get("error"))
                    or node.get("is_error") is True or node.get("ok") is False
                    or node.get("success") is False)
        failed = declared or any(isinstance(node.get(name), str)
                                 and node[name].lower() in FAILURE_STATUSES
                                 for name in FAILURE_STATUS_KEYS)
        if not failed:
            continue
        keys = list(DIAGNOSTIC_KEYS)
        if declared:
            keys.extend(PAYLOAD_KEYS)
        for key in keys:
            texts.extend(_walk_strings(node.get(key)))
    return texts


def parse_worker_log(text: str) -> Dict[str, Any]:
    """Extract model routing, tool-use and token evidence from the worker JSONL stream.

    Unsupported-tool detection is deliberately narrow: an explicit error/failure event or a
    raw diagnostic line, never successful command output, command text, reasoning or messages.
    Model, thread, tool and terminal-failure evidence comes only from the documented event
    locations: a model name, item type or event type that a payload merely quotes is data.
    """
    models: List[str] = []
    started: set = set()
    completed: set = set()
    stageless: set = set()
    anonymous = 0
    anonymous_started = 0
    anonymous_completed = 0
    thread_id: Optional[str] = None
    terminal: List[str] = []
    unsupported = False
    tokens: Dict[str, Optional[int]] = {"input_tokens": None, "output_tokens": None}
    for line in text.splitlines():
        line = line.strip()
        event: Any = None
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
        if event is None:
            if line and UNSUPPORTED_RE.search(line) and DIAGNOSTIC_RE.search(line):
                unsupported = True
            continue
        for value in observation.event_models(event):
            if value not in models:
                models.append(value)
        for value in _walk_key(event, "input_tokens") + _walk_key(event, "prompt_tokens"):
            if isinstance(value, int):
                tokens["input_tokens"] = value
        for value in _walk_key(event, "output_tokens") + _walk_key(event, "completion_tokens"):
            if isinstance(value, int):
                tokens["output_tokens"] = value
        identified = observation.tool_event(event)
        if identified is not None:
            stage, key = identified
            if key is None:
                # Without an id nothing can be deduplicated, so each observation counts once.
                anonymous += 1
                if stage == "started":
                    anonymous_started += 1
                elif stage == "completed":
                    anonymous_completed += 1
            elif stage == "started":
                started.add(key)
            elif stage == "completed":
                completed.add(key)
            else:
                stageless.add(key)
        thread = observation.event_thread_id(event)
        if thread and thread_id is None:
            thread_id = thread
        failure_type = observation.terminal_failure(event)
        if failure_type and failure_type not in terminal:
            terminal.append(failure_type)
        if any(UNSUPPORTED_RE.search(item) for item in _failure_texts(event)):
            unsupported = True
    return {
        "observed_models": sorted(models),
        "thread_id": thread_id,
        "tool_call_events": len(started | completed | stageless) + anonymous,
        "tool_started_count": len(started) + anonymous_started,
        "tool_completed_count": len(completed) + anonymous_completed,
        "unsupported_tools": unsupported,
        "terminal_failure": bool(terminal),
        "terminal_failures": sorted(terminal),
        **observation.parse_usage(text),
    }


def model_routing(evidence: Dict[str, Any], alias: str) -> str:
    observed = evidence.get("observed_models") or []
    if not observed:
        return "absent"
    if observed == [alias]:
        return "ok"
    return "mismatch"


# --------------------------------------------------------------------------------------
# Independent verification inside the Codex sandbox
# --------------------------------------------------------------------------------------
# The installed Codex CLI will not build a sandbox policy without a *named* profile, so the
# profile and its exact overrides are pinned to the command that was verified to run on this
# machine. There is deliberately no unsandboxed fallback: when the probe fails, every
# verification command is reported blocked instead of being run with full user privileges.
SANDBOX_CONFIG_ARGS = (
    "-c", f'permissions.{SANDBOX_PROFILE}.extends=":workspace"',
    "-c", f"permissions.{SANDBOX_PROFILE}.network.enabled=false",
)


def sandbox_argv(codex_bin: str, worktree: Path, argv: Sequence[str]) -> List[str]:
    """Wrap an untrusted command in the named workspace-write, network-disabled sandbox."""
    return [codex_bin, "sandbox", "-P", SANDBOX_PROFILE, "-C", str(worktree),
            *SANDBOX_CONFIG_ARGS, "--", *argv]


def sandbox_argv_template(codex_bin: str) -> List[str]:
    return [codex_bin, "sandbox", "-P", SANDBOX_PROFILE, "-C", "<worktree>",
            *SANDBOX_CONFIG_ARGS, "--", "<command>"]


def verification_argv(sandbox: Dict[str, Any], codex_bin: str, worktree: Path,
                      argv: Sequence[str]) -> List[str]:
    """Return the command exactly as this backend runs it.

    The default full-host backend runs the contract argv directly, so the returned argv is the
    plain command and the log records what really executed rather than a wrapper that is not
    there. The legacy backends stay reachable through an explicit backend: an Apple job wraps
    the command in the Seatbelt profile that was probed for it, and the legacy Codex
    workspace-write backend wraps it in ``codex sandbox``.
    """
    backend = sandbox.get("backend")
    if backend == FULL_HOST_BACKEND:
        return list(argv)
    if backend != apple.BACKEND:
        return sandbox_argv(codex_bin, worktree, argv)
    profile = str(sandbox.get("apple_profile") or "")
    if not profile:
        raise BlockedError("the Apple Seatbelt profile is missing from this job, so the "
                           "verification command is blocked rather than run unconfined")
    try:
        return apple.sandbox_argv(Path(profile), argv)
    except apple.ApplePolicyError as error:
        raise BlockedError(f"Apple Seatbelt refused the verification command: {error}") from error


def full_host_env(env: Dict[str, str]) -> Dict[str, str]:
    """Plain verification environment for the full-host backend: no confinement redirects.

    Nothing is rerouted into a job scratch directory because nothing is confined, so
    ``HOME``/``CODEX_HOME`` and the Foundation, temp and module-cache locations stay exactly as
    the job had them. Only ``IBToolNeverDeque=1`` is set -- forcibly, overriding any incoming
    value -- so actool/ibtool run in-process and cannot reuse an ``ibtoold`` that an earlier job
    started with different paths.
    """
    bounded = dict(env)
    # Forced, not defaulted: a hostile or stale incoming value must never let actool/ibtool
    # reuse an ibtoold that an earlier job started with different paths.
    bounded["IBToolNeverDeque"] = "1"
    return bounded


def _probe_codex_sandbox(codex_bin: str, worktree: Path, env: Dict[str, str],
                         secrets: Sequence[str],
                         deadline: Optional[Deadline] = None) -> Dict[str, Any]:
    """Legacy backend: the Codex workspace-write sandbox must run a trivial command."""
    template = sandbox_argv_template(codex_bin)
    timeout = deadline.cap(PROBE_TIMEOUT_SECONDS) if deadline is not None else PROBE_TIMEOUT_SECONDS
    if timeout < MIN_ATTEMPT_BUDGET_SECONDS:
        return {"available": False, "profile": SANDBOX_PROFILE, "network": "disabled",
                "backend": WORKSPACE_SANDBOX_BACKEND, "extra_writable_roots": [],
                "detail": f"the total job budget has only {timeout:.1f}s left, so the sandbox "
                          "probe was not run and no verification command will be",
                "argv_template": template}
    argv = sandbox_argv(codex_bin, worktree, ["/bin/echo", PROBE_MARKER])
    try:
        result = run_bounded(argv, cwd=worktree, env=env, timeout=timeout)
    except OSError as error:
        return {"available": False, "profile": SANDBOX_PROFILE, "network": "disabled",
                "backend": WORKSPACE_SANDBOX_BACKEND, "extra_writable_roots": [],
                "detail": f"cannot invoke {codex_bin} sandbox: {error}", "argv_template": template}
    stdout = result["stdout"].decode("utf-8", "replace")
    available = result["returncode"] == 0 and PROBE_MARKER in stdout and not result["timed_out"]
    detail = (stdout + result["stderr"].decode("utf-8", "replace")).strip()
    if result["timed_out"]:
        detail = f"sandbox probe timed out after {timeout:.1f}s: {detail}"
    return {"available": available, "profile": SANDBOX_PROFILE, "network": "disabled",
            "backend": WORKSPACE_SANDBOX_BACKEND, "extra_writable_roots": [],
            "detail": redact(detail[-500:], secrets) or ("probe ok" if available else "probe failed"),
            "probe_returncode": result["returncode"], "probe_timed_out": result["timed_out"],
            "argv_template": template}


def probe_sandbox(codex_bin: str, worktree: Path, env: Dict[str, str],
                  secrets: Sequence[str] = (),
                  deadline: Optional[Deadline] = None,
                  backend: str = FULL_HOST_BACKEND) -> Dict[str, Any]:
    """Prove a trivial command can actually run before any test command is trusted.

    The default backend is full-host: the trivial command is executed directly, with no Codex
    sandbox and no Seatbelt profile, so the metadata describes what really happened -- the
    command is available, the network is enabled and nothing confined it. Passing
    ``WORKSPACE_SANDBOX_BACKEND`` explicitly keeps the legacy confined probe available.
    """
    if backend == WORKSPACE_SANDBOX_BACKEND:
        return _probe_codex_sandbox(codex_bin, worktree, env, secrets, deadline)
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


def run_verification(task: Task, worktree: Path, env: Dict[str, str], codex_bin: str,
                     sandbox: Dict[str, Any], secrets: Sequence[str],
                     deadline: Deadline) -> List[Dict[str, Any]]:
    """Run each contract command, or report it blocked, exactly as the backend runs it.

    There is no per-test timeout reset: every command is capped by whatever is left of the
    single total job budget, so ``timeout_seconds`` bounds preflight, probe, attempts and all
    verification together.

    The backend comes from the probe result. The default full-host backend runs the plain
    command with the launcher's own permissions and network access and records
    ``sandboxed: false``; the legacy Codex workspace-write sandbox and the explicit Apple
    Seatbelt backend confine the command when a job selected them. Each entry reports which
    backend ran it and whether anything confined it.
    """
    results: List[Dict[str, Any]] = []
    backend = str(sandbox.get("backend") or FULL_HOST_BACKEND)
    full_host = backend == FULL_HOST_BACKEND
    apple_mode = backend == apple.BACKEND
    apple_scratch = str(sandbox.get("apple_scratch") or "") if apple_mode else ""
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
        if apple_mode:
            if not sandbox.get("apple_available") or not apple_scratch:
                detail = str(sandbox.get("apple_detail")
                             or "the Apple readiness probe did not run")
                results.append({
                    "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                    "backend": backend,
                    "reason": f"apple seatbelt verification is unavailable: {detail}; untrusted "
                              "commands are never run without the sandbox",
                })
                continue
        elif full_host:
            if not sandbox.get("available"):
                detail = str(sandbox.get("detail")
                             or "the full-host readiness command did not run")
                results.append({
                    "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                    "backend": backend, "confinement": FULL_HOST_CONFINEMENT,
                    "reason": f"full-host execution is unavailable: {detail}",
                })
                continue
        elif not sandbox.get("available"):
            results.append({
                "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                "backend": backend,
                "reason": "codex sandbox is unavailable; untrusted commands are never run "
                          "without the sandbox",
            })
            continue
        timeout = deadline.cap(float(task.timeout_seconds))
        try:
            wrapped = verification_argv(sandbox, codex_bin, worktree, argv)
            command_env = (apple.verification_env(env, Path(apple_scratch)) if apple_mode
                           else full_host_env(env) if full_host else env)
        except (BlockedError, apple.ApplePolicyError) as error:
            results.append({
                "command": argv, "sandboxed": False, "status": "blocked", "returncode": None,
                "backend": backend, "reason": redact(str(error), secrets),
            })
            continue
        result = run_bounded(wrapped, cwd=worktree, env=command_env, timeout=timeout)
        output = redact((result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace"), secrets)
        passed = result["returncode"] == 0 and not result["timed_out"]
        entry: Dict[str, Any] = {
            "command": argv, "sandboxed": not full_host, "log": str(index),
            "backend": backend,
            "confinement": (FULL_HOST_CONFINEMENT if full_host
                            else str(sandbox.get("apple_argv_prefix") or backend)),
            "status": "passed" if passed else "failed",
            "returncode": result["returncode"], "timed_out": result["timed_out"],
            "elapsed_seconds": result["elapsed_seconds"], "stdout_tail": output[-1000:],
            "output_bytes": len(output.encode("utf-8")), "output_text": output,
            "termination": result["termination"],
        }
        if result["timed_out"]:
            entry["reason"] = (f"verification command timed out after {timeout:.1f}s of the "
                               "shared job budget")
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
    model_settings_path: Path = OMLX_MODEL_SETTINGS
    list_models: Callable[[str, str], List[str]] = fetch_model_ids
    verify: bool = True
    attempt_timeout_cap: Optional[float] = policy.ATTEMPT_SECONDS
    apple_simulator_udid: Optional[str] = None
    maximum: int = 1
    native_preflight_plan: Optional[Path] = None
    preflight_only: bool = False
    workstream: str = "default"
    job_kind: str = "implementation"
    tool_config: Optional[Path] = None


def new_report(options: JobOptions) -> Dict[str, Any]:
    task = options.task
    return {
        "schema_version": 1,
        "launcher_pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_id": f"{_now_stamp()}-{task.task_id}-{uuid.uuid4().hex[:6]}",
        "task_id": task.task_id,
        "status": "worker_failed",
        "failure_reason": None,
        "endpoint": ENDPOINT,
        "canonical_model": CANONICAL_MODEL,
        "api_model": EXPECTED_ALIAS,
        "model_fallback": False,
        "provider": PROVIDER_ID,
        "sandbox_mode": "danger-full-access",
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
        "primary_fingerprint_before": None,
        "primary_fingerprint_after": None,
        "budget": {"total_seconds": float(task.timeout_seconds), "elapsed_seconds": 0.0,
                   "remaining_seconds": float(task.timeout_seconds), "exceeded": False},
        "workstream": options.workstream,
        "job_kind": options.job_kind,
        "native_preflight": None,
        "preflight": {},
        "sandbox": {},
        "attempts": [],
        "tests": [],
        "changed_paths": [],
        "violations": [],
        "ignored_paths": [],
        "patch_path": None,
        "patch_bytes": 0,
        "patch_excerpt": None,
        "diffstat": "",
        "token_counts": {"input_tokens": None, "output_tokens": None},
        "elapsed_seconds": 0.0,
        "logs": [],
        "limitations": list(LIMITATIONS),
    }


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _read_text(path: Path, secrets: Sequence[str]) -> str:
    try:
        return redact(path.read_text(encoding="utf-8", errors="replace"), secrets)
    except OSError:
        return ""


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


def _policy_check(argv: Sequence[str]) -> None:
    """Refuse to launch a worker whose pinned local policy drifted in any way."""
    problems = missing_config_pins(argv)
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
    previous.setdefault("job_started_at", previous["started_at"])
    previous.update(launcher_pid=report["launcher_pid"],
                    active=phase != report.get("status"),
                    job_id=report["job_id"], status=phase, phase=phase,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    workstream=report.get("workstream"), job_kind=report.get("job_kind"))
    observation._atomic_json(path, previous)


def native_gate(options, report, worktree, sandbox, verify_env, secrets, deadline):
    directory = Path(report["report_dir"])
    progress(report, "native_preflight")
    gate_started = time.monotonic()
    counter = 0
    def confined(argv):
        nonlocal counter
        counter += 1
        wrapped = verification_argv(sandbox, options.codex_bin, worktree, argv)
        # Full-host native qualification runs the real toolchain with the plain verification
        # environment; the legacy confined backend keeps its Foundation-home sandbox.
        env = (full_host_env(verify_env)
               if str(sandbox.get("backend")) == FULL_HOST_BACKEND
               else apple.verification_env(verify_env, Path(sandbox["apple_scratch"])))
        result = run_bounded(wrapped, cwd=worktree, env=env,
                             timeout=deadline.require("native preflight command"))
        output = redact((result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace"), secrets)
        log = directory / f"native-command-{counter}.log"
        log.write_text(output, encoding="utf-8")
        report["logs"].append(str(log))
        # All evidence returned to native qualification is sanitized too.
        return dict(result, stdout=redact(result["stdout"].decode("utf-8", "replace"), secrets),
                    stderr=redact(result["stderr"].decode("utf-8", "replace"), secrets))
    try:
        evidence = native.qualify(options.native_preflight_plan, worktree, directory,
                                  options.apple_simulator_udid, confined, deadline)
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
    report["preflight"] = run_preflight(
        settings_path=options.settings_path, model_settings_path=options.model_settings_path,
        list_models=options.list_models,
    )
    report["api_model"] = report["preflight"]["api_model"]
    api_key = read_api_key(options.settings_path)
    # The caller's list already holds the credentials selected by the tools manifest: it grows
    # here instead of being reset, so nothing collected before the first failure is forgotten.
    if secrets is None:
        secrets = []
    if api_key not in secrets:
        secrets.append(api_key)

    if options.native_preflight_plan is not None and options.apple_simulator_udid is None:
        raise BlockedError("native preflight plan requires --apple-simulator-udid; plan cannot be ignored")
    if options.preflight_only and options.apple_simulator_udid is None:
        raise BlockedError("native --preflight-only requires --apple-simulator-udid and a native plan")
    if options.apple_simulator_udid is not None:
        if not options.verify or options.native_preflight_plan is None:
            raise BlockedError("native jobs require --native-preflight-plan and verification; metadata probes and --no-verify cannot bypass native qualification")
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

    env, stripped_proxies, cloud_names = worker_env(api_key)
    if options.apple_simulator_udid is not None:
        # Native toolchain pin for the worker itself: ibtool/actool run in-process. HOME and
        # CODEX_HOME stay exactly as the job had them, so the real simctl/Xcode state is used.
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
        "codex_home": env.get("CODEX_HOME", "default (~/.codex), unchanged"),
    }
    if options.apple_simulator_udid is not None:
        report["environment"]["native_env"] = {
            "IBToolNeverDeque": env.get("IBToolNeverDeque"),
            "apple_simulator_udid": str(options.apple_simulator_udid),
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
            overrides=config_overrides(report["api_model"]), deadline=deadline,
            report_dir=report_dir)
    except ToolProbeError as error:
        raise BlockedError(f"required MCP tools could not be probed: {error}") from error
    # Self-check the pinned policy before the first subprocess: drift is a refusal, not a
    # degraded run. The sandbox probe runs on the same total budget as everything else.
    _policy_check(worker_argv(options.codex_bin, worktree, report["api_model"],
                              report_dir / "policy-check.last-message.txt",
                              loaded["overrides"], list(loaded["environment"])))
    sandbox = probe_sandbox(options.codex_bin, worktree, verify_env, secrets, deadline)
    if not options.verify:
        sandbox = dict(sandbox)
        sandbox["verification"] = "skipped by flag"
    report["sandbox"] = sandbox
    if options.apple_simulator_udid is not None:
        sandbox = _apple_opt_in(options, report, report_dir, worktree, sandbox, verify_env,
                                secrets, deadline)

    if options.apple_simulator_udid is not None:
        native_gate(options, report, worktree, sandbox, verify_env, secrets, deadline)
        if options.preflight_only:
            report["status"] = "ready"
            report["qualification_only"] = True
            report["primary_fingerprint_after"] = primary_fingerprint(task.repo_path)
            report["primary_unchanged"] = report["primary_fingerprint_after"] == report["primary_fingerprint_before"]
            if not report["primary_unchanged"]:
                report["status"] = "audit_violation"
            return

    # The prompt is built per attempt: only here is the effective worker cutoff known, and a
    # retry must see the budget the earlier attempt and verification actually consumed.
    feedback: Optional[str] = None
    worker_ok = False
    tests: List[Dict[str, Any]] = []
    audit: Optional[Audit] = None
    stream = report.get("_policy")
    maximum_attempts = min(task.max_attempts, report.get("workstream_admission", {}).get("max_attempts", task.max_attempts))
    for attempt_number in range(1, maximum_attempts + 1):
        try:
            if stream is not None:
                stream.before_attempt(report["job_id"])
            remaining = deadline.require(f"attempt {attempt_number}")
        except policy.PolicyStop as error:
            report["failure_reason"] = str(error)
            report["policy_stop"] = True
            break
        except DeadlineExpired as error:
            report["attempts"].append({"attempt": attempt_number, "skipped": str(error)})
            report["failure_reason"] = (
                f"the {deadline.total_seconds:.0f}s total job budget was exhausted before "
                f"attempt {attempt_number}")
            break
        reserve = min(60.0, max(5.0, deadline.total_seconds * .20)) if options.verify else 0.0
        if remaining <= reserve + 1:
            report["failure_reason"] = "worker admission stopped to preserve independent verification time"
            break
        attempt_cap = (float(options.attempt_timeout_cap)
                       if options.attempt_timeout_cap is not None else None)
        attempt_timeout = deadline.cap(remaining - reserve)
        if attempt_cap is not None:
            attempt_timeout = min(attempt_timeout, attempt_cap)
        prompt = build_prompt(task, worktree, feedback,
                              apple_udid=options.apple_simulator_udid, deadline=deadline,
                              attempt_timeout=attempt_timeout, reserve=reserve,
                              attempt_cap=attempt_cap)
        if loaded["prompt"]:
            prompt += "\n" + loaded["prompt"] + "\n"

        jsonl_path = report_dir / f"attempt-{attempt_number}.jsonl"
        prompt_path = report_dir / f"attempt-{attempt_number}.prompt.txt"
        stderr_path = report_dir / f"attempt-{attempt_number}.stderr.txt"
        last_path = report_dir / f"attempt-{attempt_number}.last-message.txt"
        prompt_path.write_text(redact(prompt, secrets), encoding="utf-8")
        argv = worker_argv(options.codex_bin, worktree, report["api_model"], last_path,
                           loaded["overrides"], list(loaded["environment"]))
        _policy_check(argv)
        patch_before = audit_worktree(worktree, base_commit, task.allowed_paths, secrets, scratch_dir=report_dir).patch
        observer = observation.RunObserver(report_dir, secrets=secrets,
                                           attempt=attempt_number, job_id=report["job_id"],
                                           job={"started_at": report.get("started_at"),
                                                "workstream": report.get("workstream"),
                                                "job_kind": report.get("job_kind")})
        context_token = _RUN_OBSERVER.set(observer)
        try:
            if stream is not None:
                stream.start_attempt(report["job_id"])
            report["_active_attempt"] = {"before_patch": patch_before}
            result = run_bounded(argv, cwd=worktree, env=env, timeout=attempt_timeout,
                                 stdin_data=prompt.encode("utf-8"))
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
        observer.finish(result=result)
        stdout_text = redact(result["stdout"].decode("utf-8", "replace"), secrets)
        stderr_text = redact(result["stderr"].decode("utf-8", "replace"), secrets)
        jsonl_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        evidence = parse_worker_log(stdout_text)
        report["tooling"] = tools.mark_observations(report.get("tooling"), stdout_text)
        observer.record_event_evidence(evidence)
        routing = model_routing(evidence, report["api_model"])
        timeline = observer.snapshot()["usage"]
        for field_name in ("started_at", "finished_at", "elapsed_seconds"):
            evidence["usage"][field_name] = timeline[field_name]
        if (result.get("timed_out") or result.get("returncode") != 0
                or evidence["terminal_failures"]):
            evidence["usage"]["complete"] = False
            if evidence["usage"]["availability"] == "complete":
                evidence["usage"]["availability"] = "partial"
        # Check actual scoped changes BEFORE verification can create its own files.
        after_worker = audit_worktree(worktree, base_commit, task.allowed_paths, secrets, scratch_dir=report_dir)
        has_patch = bool(after_worker.patch) and after_worker.patch != patch_before and not after_worker.violations
        if stream is not None:
            report["workstream_progress"] = stream.record_attempt(report["job_id"], has_patch=has_patch)
        report.pop("_active_attempt", None)
        attempt = {
            "attempt": attempt_number,
            "returncode": result["returncode"],
            "timed_out": result["timed_out"],
            "elapsed_seconds": result["elapsed_seconds"],
            "verification_reserve_seconds": reserve,
            "worker_timeout_seconds": attempt_timeout,
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
            "token_counts": evidence["token_counts"],
            "stderr_excerpt": _tail(stderr_text),
            "last_message_excerpt": _tail(_read_text(last_path, secrets)),
            "logs": [str(prompt_path), str(jsonl_path), str(stderr_path), str(last_path)],
        }
        report["logs"].extend(attempt["logs"])
        if evidence["unsupported_tools"]:
            attempt["failure_hint"] = "worker reported an unsupported tool; stopping without any model switch"
            report["attempts"].append(attempt)
            report["failure_reason"] = attempt["failure_hint"]
            break
        report["attempts"].append(attempt)

        if options.verify:
            progress(report, "verification")
            tests = run_verification(task, worktree, verify_env, options.codex_bin, sandbox,
                                     secrets, deadline)
            for test in tests:
                text = test.pop("output_text", "")
                # Keep the evidence for anything that actually ran, confined or not: an
                # executed full-host command is the primary evidence of a full-host job.
                if test.get("status") not in ("passed", "failed"):
                    continue
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
                               scratch_dir=report_dir)
        report["head_commit"] = audit.head_commit
        report["changed_paths"] = audit.changed_paths
        report["violations"] = audit.violations

        worker_ok = (result["returncode"] == 0 and not result["timed_out"]
                     and routing != "mismatch" and not evidence["terminal_failure"])
        tests_ok = (all(test.get("status") == "passed" for test in tests) if options.verify else True)
        if routing == "mismatch" or (worker_ok and tests_ok and not audit.violations):
            break
        if attempt_number < maximum_attempts:
            feedback = summarize_failure(attempt, tests, audit.violations)

    if audit is None:
        # No attempt reached verification (unsupported tool, exhausted budget, ...). Audit
        # what the worker left behind anyway, still against the original base commit.
        audit = audit_worktree(worktree, base_commit, task.allowed_paths, secrets,
                               scratch_dir=report_dir)
        report["head_commit"] = audit.head_commit
    patch_path = report_dir / "changes.patch"
    patch_path.write_bytes(audit.patch)
    report["patch_path"] = str(patch_path)
    report["patch_bytes"] = len(audit.patch)
    report["patch_excerpt"] = audit.patch[:PATCH_REPORT_BYTES].decode("utf-8", "replace")
    report["diffstat"] = audit.diffstat
    report["changed_paths"] = audit.changed_paths
    report["violations"] = audit.violations
    report["ignored_paths"] = audit.ignored_paths
    report["dirty_primary_after"] = dirty_primary_files(task.repo_path)
    report["primary_fingerprint_after"] = primary_fingerprint(task.repo_path)
    report["primary_unchanged"] = (report["primary_fingerprint_after"]
                                   == report["primary_fingerprint_before"])
    left = deadline.remaining()
    report["budget"] = {
        "total_seconds": deadline.total_seconds,
        "elapsed_seconds": round(deadline.total_seconds - max(left, 0.0), 2),
        "remaining_seconds": round(left, 2),
        "exceeded": deadline.exceeded(),
    }

    attempts = [item for item in report["attempts"] if "returncode" in item]
    report.update(observation.sum_usage(attempts))

    report["status"] = "blocked" if report.get("policy_stop") else decide_status(report, worker_ok)


def decide_status(report: Dict[str, Any], worker_ok: bool) -> str:
    """Never report success without passing verification evidence."""
    attempts = [item for item in report["attempts"] if "returncode" in item]
    statuses = [test.get("status") for test in report["tests"]]
    all_passed = bool(statuses) and all(status == "passed" for status in statuses)
    budget_exceeded = bool((report.get("budget") or {}).get("exceeded"))
    # A changed primary checkout is a policy violation no matter what else happened, and it
    # is checked first so it can never be reported as success.
    if report.get("primary_unchanged") is False:
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
    return "ok"


STATUS_REASONS = {
    "contract_invalid": "the task contract was rejected",
    "preflight_failed": "local endpoint, alias or model availability preflight failed",
    "blocked": "the job was blocked before the worker could run",
    "worker_failed": "the worker exited unsuccessfully without passing verification",
    "audit_violation": "the worker changed something outside allowed_paths or touched the "
                       "primary checkout",
    "tests_failed": "verification commands failed after the worker finished",
    "verification_blocked": "verification produced no passing evidence",
    "timeout": "the total job budget ran out before passing verification",
    "model_mismatch": "the worker was served by a model other than the pinned canonical alias",
}


def describe_failure(report: Dict[str, Any]) -> str:
    """Every non-ok status gets a concrete reason, not just a status word."""
    status = report.get("status")
    if status == "ok":
        return ""
    if report.get("failure_reason"):
        return str(report["failure_reason"])
    parts: List[str] = []
    if report.get("primary_unchanged") is False:
        parts.append("the primary checkout changed during the job: HEAD, index or the content "
                     "of a dirty file differs from before the job started")
    for violation in report.get("violations", [])[:5]:
        parts.append(f"audit violation {violation.get('path')} ({violation.get('reason')})")
    for attempt in report.get("attempts", []):
        if attempt.get("model_routing") == "mismatch":
            parts.append(f"attempt {attempt.get('attempt')} observed model(s) "
                         f"{attempt.get('observed_models')} instead of the pinned alias "
                         f"{report.get('api_model')}")
        if attempt.get("unsupported_tools"):
            detail = attempt.get("stderr_excerpt") or "the worker stream reported an unsupported tool"
            parts.append(f"attempt {attempt.get('attempt')} reported unsupported tool(s): {detail}")
        if attempt.get("terminal_failures"):
            parts.append(f"attempt {attempt.get('attempt')} ended on terminal event(s) "
                         f"{attempt.get('terminal_failures')} regardless of exit code "
                         f"{attempt.get('returncode')}")
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
    stream = None
    claimed = False
    try:
        if _inside(options.state_dir, options.task.repo_path):
            raise ContractError(
                f"state dir {options.state_dir} must live outside the repository "
                f"{options.task.repo_path}; reports and logs must not be worker-writable"
            )
        try:
            stream = policy.Workstream(options.state_dir, repo_identity(options.task.repo_path), options.workstream, options.job_kind)
            if not options.preflight_only:
                admission = stream.claim(report["job_id"], deadline.require("workstream admission"), options.task.max_attempts)
                claimed = True
                report["workstream_admission"] = admission
                if admission["job_budget_seconds"] < deadline.remaining():
                    deadline.end = min(deadline.end, time.monotonic() + admission["job_budget_seconds"])
        except (policy.PolicyStop, ValueError) as error:
            raise BlockedError(str(error)) from error
        report["_policy"] = stream if claimed else None
        key_error: Optional[Exception] = None
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
            if key_error is not None:
                raise key_error
            _execute(options, report, deadline, lease, secrets=secrets)
    except LauncherError as error:
        report["status"] = error.status
        report["failure_reason"] = redact(str(error), secrets)
    except Exception as error:  # noqa: BLE001 - report, never rethrow raw
        report["status"] = "worker_failed"
        report["failure_reason"] = redact(f"unexpected {error.__class__.__name__}: {error}", secrets)
    finally:
        report.pop("_policy", None)
        unfinished = report.pop("_active_attempt", None)
        if report.get("worktree") and report.get("primary_fingerprint_before"):
            try:
                final_audit = audit_worktree(Path(report["worktree"]), report["base_commit"], options.task.allowed_paths, secrets, scratch_dir=Path(report["report_dir"]))
                report["changed_paths"] = final_audit.changed_paths
                report["violations"] = final_audit.violations
                report["head_commit"] = final_audit.head_commit
                if unfinished and claimed:
                    stream.record_attempt(report["job_id"], has_patch=bool(final_audit.patch) and final_audit.patch != unfinished["before_patch"] and not final_audit.violations)
                report["primary_fingerprint_after"] = primary_fingerprint(options.task.repo_path)
                report["primary_unchanged"] = report["primary_fingerprint_after"] == report["primary_fingerprint_before"]
                if final_audit.violations or not report["primary_unchanged"]:
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
        ensure_report_dir(options, report)
        attempts = [a for a in report["attempts"] if "returncode" in a]
        report.update(observation.sum_usage(attempts))
        report["timing"] = {"job_seconds": report["elapsed_seconds"],
                            "worker_seconds": round(sum(a["elapsed_seconds"] for a in attempts), 3),
                            "verification_seconds": round(sum(t.get("elapsed_seconds", 0) or 0 for a in attempts for t in a.get("tests", [])), 3),
                            "native_preflight_seconds": report.get("native_preflight_seconds", 0.0)}
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-worker",
        description="Run one local Codex CLI worker against the pinned loopback oMLX model.",
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
    run.add_argument("--apple-simulator-udid", metavar="UUID", default=None,
                     help="opt in: native Apple jobs stay on the full-host backend and are "
                          "scoped to this tvOS simulator UUID; the native gate proves "
                          "build/install/launch/test, and the model pins are unchanged")
    run.add_argument("--native-preflight-plan", type=Path)
    run.add_argument("--preflight-only", action="store_true", help="qualify native baseline without starting a worker")
    run.add_argument("--workstream", default="default", help="stable scope name shared across retries and repair jobs")
    run.add_argument("--job-kind", choices=("implementation", "repair"), default="implementation")
    run.add_argument("--tool-config", type=Path, default=None,
                     help="optional MCP tools manifest for this task: the manifest names the "
                          "credentials it needs and their values never reach argv, evidence or "
                          "the prompt")
    run.add_argument("--settings", default=str(OMLX_SETTINGS))
    run.add_argument("--model-settings", default=str(OMLX_MODEL_SETTINGS))
    run.add_argument("--json", action="store_true", help="print the full report JSON")

    status = sub.add_parser("status", help="read progress and actual usage without starting a worker")
    status.add_argument("--state-dir")
    status.add_argument("--job-id")
    check = sub.add_parser("preflight", help="model metadata only; does not qualify native execution")
    check.add_argument("--settings", default=str(OMLX_SETTINGS))
    check.add_argument("--model-settings", default=str(OMLX_MODEL_SETTINGS))
    return parser


def summary_lines(report: Dict[str, Any]) -> List[str]:
    lines = [
        f"status: {report['status']} (exit {report.get('exit_code')})",
        f"model: {report.get('canonical_model')} -> {report.get('api_model')} @ {report.get('endpoint')}",
        f"worktree: {report.get('worktree') or 'not created'}",
        f"report: {report.get('report_dir') or report.get('report_path') or 'not written'}",
        f"changed paths: {len(report.get('changed_paths', []))}",
    ]
    for violation in report.get("violations", [])[:10]:
        lines.append(f"violation: {violation['path']} ({violation['reason']})")
    for test in report.get("tests", []):
        lines.append(f"test {json.dumps(test['command'])}: {test['status']}")
    sandbox = report.get("sandbox") or {}
    if sandbox.get("backend") == FULL_HOST_BACKEND:
        lines.append(f"backend: {FULL_HOST_BACKEND} confined=no network=enabled "
                     f"simulator {sandbox.get('apple_simulator_udid') or 'not selected'}")
    elif sandbox.get("backend") == apple.BACKEND:
        lines.append(f"apple backend: {apple.SANDBOX_EXEC} profile sha256 "
                     f"{str(sandbox.get('apple_profile_sha256'))[:12]} simulator "
                     f"{sandbox.get('apple_simulator_udid')} "
                     f"{'ready' if sandbox.get('apple_available') else 'NOT READY'}")
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


def status_snapshot(item):
    """Read process liveness without treating missing output as a stalled model."""
    item = dict(item)
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


def main(argv: Optional[Sequence[str]] = None, *,
         list_models: Callable[[str, str], List[str]] = fetch_model_ids) -> int:
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
        if args.command == "preflight":
            evidence = run_preflight(
                settings_path=Path(args.settings), model_settings_path=Path(args.model_settings),
                list_models=list_models,
            )
            print(json.dumps(evidence, indent=2))
            return 0

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
            model_settings_path=Path(os.path.expanduser(args.model_settings)),
            verify=not args.no_verify,
            list_models=list_models,
            apple_simulator_udid=udid,
            maximum=maximum,
            native_preflight_plan=args.native_preflight_plan,
            preflight_only=args.preflight_only,
            workstream=args.workstream,
            job_kind=args.job_kind,
            tool_config=args.tool_config,
        )
        report = run_job(options)
    except LauncherError as error:
        print(json.dumps({"status": error.status, "failure_reason": str(error)}, indent=2))
        return EXIT_CODES.get(error.status, 4)
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "contract_invalid",
                          "failure_reason": f"cannot read task contract: {error}"}, indent=2))
        return EXIT_CODES["contract_invalid"]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(summary_lines(report)))
    return int(report.get("exit_code", 4))


if __name__ == "__main__":
    sys.exit(main())
