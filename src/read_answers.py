"""Schema-subset validation and source-citation checks for read jobs."""

import re
from pathlib import Path
from typing import Any, Dict, List

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minItems", "maxItems", "maxLength", "minimum", "maximum", "description",
})


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
