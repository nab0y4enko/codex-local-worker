"""Schema-subset validation and source-citation checks for read jobs."""

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence, Tuple

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minItems", "maxItems", "maxLength", "minimum", "maximum", "description",
})


_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
MAX_REFERENCE_OUTPUT_BYTES = 64 * 1024 + 1  # helper's 64 KiB JSON cap plus its newline
MAX_REFERENCE_OUTPUTS = 32
MAX_REFERENCE_TOTAL_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_FILE_BYTES = 2 * 1024 * 1024  # same source-read ceiling as source_citations.py


def extract_json_text(message: str) -> str:
    """The JSON text of a worker's final message, tolerating a code fence around it.

    Local reads send no response-format constraint (it stops the model from calling tools), so
    a model may wrap the object in a fence or add a sentence before it. The whole message is
    used when it already parses; otherwise the last fenced block that parses as JSON. Anything
    else is returned unchanged and fails the caller's JSON check. Extraction only locates the
    text: the caller still validates the schema and verifies every citation.
    """
    def parses(text: str) -> bool:
        try:
            json.loads(text)
        except ValueError:
            return False
        return True

    if parses(message):
        return message
    for match in reversed(_FENCE.findall(message)):
        if parses(match):
            return match
    return message


def envelope_schema(answer_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a caller's answer schema with the required, typed citation envelope."""
    citation = {
        "type": "object",
        "required": ["path", "line_start", "line_end", "quote"],
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string"},
            "line_start": {"type": "integer", "minimum": 1},
            "line_end": {"type": "integer", "minimum": 1},
            "quote": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "required": ["answer", "citations"],
        "additionalProperties": False,
        "properties": {
            "answer": answer_schema,
            "citations": {"type": "array", "minItems": 1, "items": citation},
        },
    }


def reference_envelope_schema(answer_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap an answer schema whose citations refer to observed helper source entries."""
    reference = {
        "type": "object",
        "required": ["source_id"],
        "additionalProperties": False,
        "properties": {"source_id": {"type": "string"}},
    }
    return {
        "type": "object",
        "required": ["answer", "citations"],
        "additionalProperties": False,
        "properties": {
            "answer": answer_schema,
            "citations": {"type": "array", "minItems": 1, "items": reference},
        },
    }


def citation_id(citation: Dict[str, Any]) -> str:
    """Stable, readable reference for an exact relative path and line range."""
    path = citation.get("path")
    start = citation.get("line_start")
    end = citation.get("line_end")
    if (not isinstance(path, str) or not path or "\n" in path or "\r" in path
            or isinstance(start, bool) or not isinstance(start, int) or start < 1
            or isinstance(end, bool) or not isinstance(end, int) or end < start):
        raise ValueError("citation cannot be represented as a source reference")
    return "source:%s:%d-%d" % (path, start, end)


def resolve_citation_references(envelope: Any, command_outputs: Sequence[str],
                                worktree: Path, scope_paths: Sequence[str]
                                ) -> Tuple[Any, List[str]]:
    """Resolve exact source IDs from bounded helper output into verified citations.

    Full legacy citations pass through unchanged for backwards compatibility. Reference objects
    are replaced only when the ID maps to one unique citation in parsed helper output and that
    citation still verifies against both the worktree and the declared read scopes.
    """
    import copy

    normalized = copy.deepcopy(envelope)
    errors: List[str] = []
    if not isinstance(normalized, dict) or not isinstance(normalized.get("citations"), list):
        return normalized, errors
    references = []
    for index, item in enumerate(normalized["citations"]):
        if isinstance(item, dict) and "source_id" in item:
            if set(item) != {"source_id"} or not isinstance(item.get("source_id"), str) or not item["source_id"]:
                errors.append("citation %d has a malformed source reference" % index)
            else:
                references.append((index, item["source_id"]))
    if not references:
        return normalized, errors

    source_map: Dict[str, Dict[str, Any]] = {}
    ambiguous = set()
    if not isinstance(command_outputs, (list, tuple)):
        errors.append("helper outputs are not a bounded list")
        command_outputs = []
    elif len(command_outputs) > MAX_REFERENCE_OUTPUTS:
        errors.append("helper output count exceeds the limit")
        command_outputs = []
    total_bytes = 0
    try:
        root = Path(worktree).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        root = None
        errors.append("worktree root cannot be resolved for source references")

    resolved_scopes = []
    if root is not None:
        if not scope_paths or len(scope_paths) > 32:
            errors.append("declared source scopes are missing or exceed the limit")
        else:
            for raw_scope in scope_paths:
                if (not isinstance(raw_scope, str) or not raw_scope or raw_scope.startswith(("/", "~", "\\"))
                        or "\\" in raw_scope):
                    errors.append("declared source scope is not a safe relative path")
                    continue
                relative_scope = PurePosixPath(raw_scope)
                if relative_scope.is_absolute() or ".." in relative_scope.parts or relative_scope.as_posix() == ".":
                    errors.append("declared source scope is not a safe relative path")
                    continue
                try:
                    target_scope = (root / Path(*relative_scope.parts)).resolve(strict=True)
                    target_scope.relative_to(root)
                    if not target_scope.is_file() and not target_scope.is_dir():
                        raise ValueError("scope is not a file or directory")
                except (OSError, RuntimeError, ValueError):
                    errors.append("declared source scope cannot be resolved safely")
                    continue
                resolved_scopes.append(target_scope)

    for output_index, output in enumerate(command_outputs):
        if not isinstance(output, str):
            errors.append("helper output %d is not text" % output_index)
            continue
        if len(output) > MAX_REFERENCE_OUTPUT_BYTES:
            errors.append("helper output %d exceeds the output limit" % output_index)
            continue
        try:
            output_bytes = output.encode("utf-8")
        except UnicodeError:
            errors.append("helper output %d is not valid UTF-8 text" % output_index)
            continue
        if len(output_bytes) > MAX_REFERENCE_OUTPUT_BYTES:
            errors.append("helper output %d exceeds the output limit" % output_index)
            continue
        total_bytes += len(output_bytes)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            errors.append("combined helper outputs exceed the output limit")
            break
        try:
            payload = json.loads(output)
        except (ValueError, TypeError, RecursionError):
            continue
        if (not isinstance(payload, dict)
                or type(payload.get("source_citations_version")) is not int
                or payload.get("source_citations_version") != 1):
            # Command output collection includes ordinary shell output too. Only a JSON object
            # with the source helper's version marker is relevant; an absent ID is rejected below.
            continue
        files = payload.get("files")
        if not isinstance(files, list):
            errors.append("helper output %d has malformed source file entries" % output_index)
            continue
        for file_entry in files:
            if not isinstance(file_entry, dict) or not isinstance(file_entry.get("source_refs"), list):
                errors.append("helper output %d has malformed source references" % output_index)
                continue
            for source_ref in file_entry["source_refs"]:
                if (not isinstance(source_ref, dict) or set(source_ref) != {"source_id", "citation"}
                        or not isinstance(source_ref.get("source_id"), str)
                        or not isinstance(source_ref.get("citation"), dict)):
                    errors.append("helper output %d has a malformed source reference" % output_index)
                    continue
                citation = source_ref["citation"]
                try:
                    expected_id = citation_id(citation)
                except ValueError:
                    errors.append("helper output %d has an invalid citation reference" % output_index)
                    continue
                if source_ref["source_id"] != expected_id:
                    errors.append("helper output %d has a mismatched citation reference" % output_index)
                    continue
                if (root is None or not resolved_scopes
                        or not _citation_within_scopes(root, citation, resolved_scopes)
                        or _citation_failure(root, citation) is not None):
                    errors.append("helper output %d contains an unverified or out-of-scope citation" % output_index)
                    continue
                source_id = source_ref["source_id"]
                if source_id in ambiguous:
                    continue
                if source_id in source_map:
                    if source_map[source_id] != citation:
                        source_map.pop(source_id, None)
                        ambiguous.add(source_id)
                        errors.append("helper outputs contain an ambiguous source ID")
                    continue
                source_map[source_id] = citation

    for index, source_id in references:
        if source_id in ambiguous:
            errors.append("citation %d refers to an ambiguous source ID" % index)
        elif source_id not in source_map:
            errors.append("citation %d refers to an unknown source ID" % index)
        else:
            normalized["citations"][index] = dict(source_map[source_id])
    return normalized, errors


def _citation_within_scopes(root: Path, citation: Dict[str, Any], scopes: Sequence[Path]) -> bool:
    path = citation.get("path")
    if not isinstance(path, str):
        return False
    try:
        target = (root / path).resolve(strict=True)
        if not target.is_file() or target.stat().st_size > MAX_REFERENCE_FILE_BYTES:
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    for scope in scopes:
        if scope.is_file() and target == scope:
            return True
        if scope.is_dir():
            try:
                target.relative_to(scope)
                return True
            except ValueError:
                pass
    return False


def validate(instance: Any, schema: Dict[str, Any]) -> List[str]:
    """Validate the JSON Schema subset supported by contract v2.

    Messages begin with a JSON Pointer to the failing value (``/`` denotes the root).
    """
    errors: List[str] = []

    def add(pointer: str, message: str) -> None:
        errors.append((pointer or "/") + ": " + message)

    def visit(value: Any, rule: Dict[str, Any], pointer: str) -> None:
        expected = rule.get("type")
        types = expected if isinstance(expected, list) else [expected] if expected else []
        if types and not any(_is_type(value, name) for name in types):
            add(pointer, "expected %s, got %s" % (" or ".join(str(v) for v in types),
                                                    _type_name(value)))
            return

        if "enum" in rule and value not in rule["enum"]:
            add(pointer, "value is not in enum")

        if isinstance(value, dict):
            required = rule.get("required", [])
            for key in required:
                if key not in value:
                    add(_child(pointer, key), "required property is missing")
            properties = rule.get("properties", {})
            for key, child_rule in properties.items():
                if key in value:
                    visit(value[key], child_rule, _child(pointer, key))
            additional = rule.get("additionalProperties", True)
            for key, child in value.items():
                if key in properties:
                    continue
                if additional is False:
                    add(_child(pointer, key), "additional property is not allowed")
                elif isinstance(additional, dict):
                    visit(child, additional, _child(pointer, key))
        elif isinstance(value, list):
            if "minItems" in rule and len(value) < rule["minItems"]:
                add(pointer, "array has fewer than %s items" % rule["minItems"])
            if "maxItems" in rule and len(value) > rule["maxItems"]:
                add(pointer, "array has more than %s items" % rule["maxItems"])
            item_rule = rule.get("items")
            if isinstance(item_rule, dict):
                for index, child in enumerate(value):
                    visit(child, item_rule, _child(pointer, index))
        elif isinstance(value, str) and "maxLength" in rule:
            if len(value) > rule["maxLength"]:
                add(pointer, "string has more than %s characters" % rule["maxLength"])

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                add(pointer, "number is less than %s" % rule["minimum"])
            if "maximum" in rule and value > rule["maximum"]:
                add(pointer, "number is greater than %s" % rule["maximum"])

    if not isinstance(schema, dict):
        return ["/: schema must be an object"]
    visit(instance, schema, "")
    return errors


def verify_citations(worktree: Path, citations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Check each quote against its cited worktree lines without following paths outside it."""
    root = Path(worktree).resolve()
    entries = citations if isinstance(citations, list) else []
    failures: List[Dict[str, Any]] = []
    verified = 0
    for index, citation in enumerate(entries):
        reason = _citation_failure(root, citation)
        if reason is None:
            verified += 1
        else:
            failures.append({"index": index, "reason": reason})
    return {"total": len(entries), "verified": verified, "failures": failures}


def _citation_failure(root: Path, citation: Any) -> Any:
    if not isinstance(citation, dict):
        return "citation must be an object"
    path = citation.get("path")
    if not isinstance(path, str) or not path or path.startswith(("/", "~", "\\")):
        return "path must be worktree-relative"
    parts = path.replace("\\", "/").split("/")
    if ".." in parts or re.match(r"^[A-Za-z]:", path):
        return "path escapes the worktree"
    try:
        target = (root / path).resolve(strict=True)
        target.relative_to(root)
    except ValueError:
        return "path escapes the worktree"
    except (OSError, RuntimeError):
        return "path does not exist or cannot be resolved"
    if not target.is_file():
        return "path is not a file"
    start = citation.get("line_start")
    end = citation.get("line_end")
    if (isinstance(start, bool) or not isinstance(start, int) or
            isinstance(end, bool) or not isinstance(end, int) or start < 1 or end < start):
        return "line range is invalid"
    quote = citation.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return "quote must be a non-empty string"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "file cannot be read as UTF-8 text"
    if end > len(lines):
        return "line range is outside the file"
    selected = [_normalize_line(line) for line in lines[start - 1:end]]
    wanted = [_normalize_line(line) for line in quote.splitlines()]
    wanted = [line for line in wanted if line]
    if not wanted:
        return "quote must be a non-empty string"
    width = len(wanted)
    if width == 1:
        matches = any(wanted[0] in line for line in selected)
    else:
        matches = any(all(wanted[offset] in selected[start + offset]
                          for offset in range(width))
                      for start in range(0, len(selected) - width + 1))
    if not matches:
        return "quote does not match the cited lines"
    return None


def _normalize_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line.replace("\r", "")).strip()


def _child(pointer: str, value: Any) -> str:
    token = str(value).replace("~", "~0").replace("/", "~1")
    return pointer + "/" + token


def _is_type(value: Any, name: Any) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    return "number"
