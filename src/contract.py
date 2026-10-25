"""Validated job contract v2 with a v1 write-contract upgrade path."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import read_answers


MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 14400
ALLOWED_ATTEMPTS = (1, 2)
KINDS = ("read", "write", "capture")
ROUTING_ENV_PREFIXES = ("CODEX", "LOCAL_WORKER", "LOCAL_OMLX", "OMLX", "OPENAI",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REF_BAD_CHARS = re.compile(r"[\s~^:?*\[\]\\]|\.\.|@\{")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ContractError(ValueError):
    status = "contract_invalid"


@dataclass(frozen=True)
class Tier:
    kind: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None


@dataclass
class Contract:
    task_id: str
    repo_path: Path
    base_ref: str
    goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=list)
    test_commands: List[List[str]] = field(default_factory=list)
    timeout_seconds: int = 60
    max_attempts: int = 1
    kind: str = "write"
    workstream: str = ""
    environment_pass: List[str] = field(default_factory=list)
    tier: Tier = field(default_factory=lambda: Tier("local"))
    native_plan: Optional[Path] = None
    ui_review: bool = False
    question: Optional[str] = None
    scope_paths: List[str] = field(default_factory=list)
    answer_schema: Optional[Dict[str, Any]] = None
    max_answer_bytes: int = 65536
    capture: Optional[Dict[str, Any]] = None

    @classmethod
    def from_payload(cls, payload: Any) -> "Contract":
        if not isinstance(payload, dict):
            raise ContractError("contract must be a JSON object, got %s" % type(payload).__name__)

        # The original nine-field contract remains accepted and is normalized to v2.
        v1_fields = {"task_id", "repo_path", "base_ref", "goal", "acceptance_criteria",
                     "allowed_paths", "test_commands", "timeout_seconds", "max_attempts"}
        if set(payload) == v1_fields:
            payload = dict(payload, kind="write", workstream=payload["task_id"],
                           environment={"pass": []}, tier={"local": True}, ui_review=False)

        common = {"task_id", "kind", "repo_path", "base_ref", "goal", "timeout_seconds",
                  "workstream", "environment", "tier"}
        required_common = {"task_id", "kind", "repo_path", "base_ref", "goal", "timeout_seconds"}
        kind = payload.get("kind", "write")
        if kind == "write":
            allowed = common | {"acceptance_criteria", "allowed_paths", "test_commands",
                                "max_attempts", "native", "native_plan", "ui_review"}
            required = required_common | {"acceptance_criteria", "allowed_paths", "test_commands",
                                         "max_attempts"}
        elif kind == "read":
            allowed = common | {"question", "scope_paths", "answer_schema", "max_answer_bytes"}
            required = required_common | {"question", "scope_paths", "answer_schema", "max_answer_bytes"}
        elif kind == "capture":
            allowed = common | {"capture"}
            required = required_common | {"capture"}
        else:
            raise ContractError("kind: must be one of %s, got %r" % (list(KINDS), kind))

        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ContractError("unknown contract field(s): %s" % ", ".join(unknown))
        missing = sorted(required - set(payload))
        if missing:
            raise ContractError("missing contract field(s): %s" % ", ".join(missing))

        task_id = _string(payload["task_id"], "task_id")
        if not TASK_ID_RE.match(task_id):
            raise ContractError("task_id: must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        repo_raw = _string(payload["repo_path"], "repo_path")
        repo_path = Path(repo_raw)
        if not repo_path.is_absolute():
            raise ContractError("repo_path: must be absolute, got %r" % repo_raw)
        if not repo_path.is_dir():
            raise ContractError("repo_path: no such directory: %s" % repo_raw)
        base_ref = _string(payload["base_ref"], "base_ref")
        if base_ref.startswith("-") or REF_BAD_CHARS.search(base_ref):
            raise ContractError("base_ref: invalid git revision specifier %r" % base_ref)
        goal = _string(payload["goal"], "goal")
        timeout = _integer(payload["timeout_seconds"], "timeout_seconds")
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ContractError("timeout_seconds: must be between %d and %d, got %d" %
                                (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, timeout))

        workstream = _string(payload.get("workstream", task_id), "workstream")
        env = payload.get("environment", {"pass": []})
        if not isinstance(env, dict) or set(env) != {"pass"}:
            raise ContractError("environment: must contain only the 'pass' array")
        env_names = _string_list(env["pass"], "environment.pass", allow_empty=True)
        clean_env_names: List[str] = []
        for name in env_names:
            if not ENV_NAME_RE.fullmatch(name):
                raise ContractError("environment.pass: invalid environment variable name %r" % name)
            if name.startswith(ROUTING_ENV_PREFIXES):
                raise ContractError("environment.pass: routing variables cannot be passed: %s" % name)
            if name not in clean_env_names:
                clean_env_names.append(name)
        tier = _parse_tier(payload.get("tier", {"local": True}))

        kwargs: Dict[str, Any] = {"task_id": task_id, "repo_path": repo_path,
                                  "base_ref": base_ref, "goal": goal, "timeout_seconds": timeout,
                                  "kind": kind, "workstream": workstream,
                                  "environment_pass": clean_env_names, "tier": tier}
        if kind == "write":
            criteria = _string_list(payload["acceptance_criteria"], "acceptance_criteria")
            paths = []
            for item in _string_list(payload["allowed_paths"], "allowed_paths"):
                normalized = _relative_path(item, "allowed_paths[]")
                if normalized not in paths:
                    paths.append(normalized)
            if not paths:
                raise ContractError("allowed_paths: at least one path is required")
            commands = _commands(payload["test_commands"])
            attempts = _integer(payload["max_attempts"], "max_attempts")
            if attempts not in ALLOWED_ATTEMPTS:
                raise ContractError("max_attempts: must be one of %s" % list(ALLOWED_ATTEMPTS))
            native_raw = payload.get("native_plan", payload.get("native"))
            native_plan = None
            if native_raw is not None:
                if not isinstance(native_raw, str) or not native_raw.strip():
                    raise ContractError("native: must be a non-empty path")
                native_plan = Path(native_raw)
            ui_review = payload.get("ui_review", False)
            if not isinstance(ui_review, bool):
                raise ContractError("ui_review: must be a boolean")
            kwargs.update(acceptance_criteria=criteria, allowed_paths=paths,
                          test_commands=commands, max_attempts=attempts,
                          native_plan=native_plan, ui_review=ui_review)
        elif kind == "read":
            question = _string(payload["question"], "question")
            scope_paths = [_relative_path(item, "scope_paths[]") for item in
                           _string_list(payload["scope_paths"], "scope_paths", allow_empty=True)]
            schema = payload["answer_schema"]
            _validate_schema_keywords(schema)
            max_bytes = _integer(payload["max_answer_bytes"], "max_answer_bytes")
            if max_bytes < 1:
                raise ContractError("max_answer_bytes: must be positive")
            kwargs.update(question=question, scope_paths=scope_paths,
                          answer_schema=schema, max_answer_bytes=max_bytes)
        else:
            capture = payload["capture"]
            if not isinstance(capture, dict):
                raise ContractError("capture: must be an object")
            kwargs["capture"] = capture
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        tier: Dict[str, Any]
        if self.tier.kind == "cloud":
            tier = {"cloud": self.tier.model}
            if self.tier.reasoning_effort is not None:
                tier["reasoning_effort"] = self.tier.reasoning_effort
        else:
            tier = {"local": True}
        result = {"task_id": self.task_id, "kind": self.kind, "repo_path": str(self.repo_path),
                  "base_ref": self.base_ref, "goal": self.goal,
                  "timeout_seconds": self.timeout_seconds, "workstream": self.workstream,
                  "environment": {"pass": list(self.environment_pass)},
                  "tier": tier}
        if self.kind == "write":
            result.update(acceptance_criteria=self.acceptance_criteria,
                          allowed_paths=self.allowed_paths, test_commands=self.test_commands,
                          max_attempts=self.max_attempts, ui_review=self.ui_review)
            if self.native_plan is not None:
                result["native_plan"] = str(self.native_plan)
        elif self.kind == "read":
            result.update(question=self.question, scope_paths=self.scope_paths,
                          answer_schema=self.answer_schema, max_answer_bytes=self.max_answer_bytes)
        else:
            result["capture"] = self.capture
        return result


def _string(value: Any, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or "\x00" in value:
        raise ContractError("%s: must be a %sstring" %
                            (field_name, "possibly empty " if allow_empty else "non-empty "))
    return value


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("%s: must be an integer" % field_name)
    return value


def _string_list(value: Any, field_name: str, allow_empty: bool = False) -> List[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ContractError("%s: must be a%s array of strings" %
                            (field_name, " possibly empty" if allow_empty else " non-empty"))
    return [_string(item, field_name + "[]") for item in value]


def _relative_path(raw: str, field_name: str) -> str:
    if raw.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        raise ContractError("%s: must be relative to the repo root" % field_name)
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts or ".git" in parts:
        raise ContractError("%s: invalid repository-relative path %r" % (field_name, raw))
    return "/".join(parts)


def _commands(value: Any) -> List[List[str]]:
    if not isinstance(value, list) or not value:
        raise ContractError("test_commands: must contain at least one argv array")
    commands = []
    for index, argv in enumerate(value):
        if not isinstance(argv, list) or not argv:
            raise ContractError("test_commands[%d]: must be a non-empty argv array" % index)
        commands.append([_string(part, "test_commands[%d][]" % index) for part in argv])
    return commands


def _parse_tier(value: Any) -> Tier:
    if value == "local" or value == {"local": True} or value is None:
        return Tier("local")
    if not isinstance(value, dict):
        raise ContractError("tier: must be local or a cloud tier object")
    if "cloud" in value:
        if set(value) - {"cloud", "reasoning_effort"}:
            raise ContractError("tier: unknown cloud tier field")
        model = _string(value["cloud"], "tier.cloud")
        effort = value.get("reasoning_effort")
        if effort is not None:
            effort = _string(effort, "tier.reasoning_effort")
        return Tier("cloud", model, effort)
    if value == {"kind": "local"}:
        return Tier("local")
    raise ContractError("tier: cloud tier requires a non-empty model")


def _validate_schema_keywords(schema: Any, pointer: str = "") -> None:
    if not isinstance(schema, dict):
        raise ContractError("answer_schema%s: schema must be an object" % pointer)
    unknown = sorted(set(schema) - read_answers.SUPPORTED_SCHEMA_KEYWORDS)
    if unknown:
        raise ContractError("answer_schema%s: unsupported keyword(s): %s" %
                            (pointer, ", ".join(unknown)))
    types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if "type" in schema:
        declared = schema["type"]
        declared = declared if isinstance(declared, list) else [declared]
        if not declared or any(not isinstance(item, str) or item not in types for item in declared):
            raise ContractError("answer_schema%s/type: contains an unsupported JSON type" % pointer)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ContractError("answer_schema%s/required: must be an array of strings" % pointer)
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
        raise ContractError("answer_schema%s/properties: must be an object" % pointer)
    for key, child in properties.items():
        _validate_schema_keywords(child, pointer + "/properties/" + str(key))
    if "items" in schema:
        _validate_schema_keywords(schema["items"], pointer + "/items")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, (bool, dict)):
        raise ContractError("answer_schema%s/additionalProperties: must be a boolean or schema" % pointer)
    if isinstance(additional, dict):
        _validate_schema_keywords(additional, pointer + "/additionalProperties")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ContractError("answer_schema%s/enum: must be an array" % pointer)
    for key in ("minItems", "maxItems", "maxLength"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], int)
                              or schema[key] < 0):
            raise ContractError("answer_schema%s/%s: must be a non-negative integer" %
                                (pointer, key))
    for key in ("minimum", "maximum"):
        if key in schema and (isinstance(schema[key], bool) or
                              not isinstance(schema[key], (int, float))):
            raise ContractError("answer_schema%s/%s: must be a number" % (pointer, key))
    if "description" in schema and not isinstance(schema["description"], str):
        raise ContractError("answer_schema%s/description: must be a string" % pointer)
