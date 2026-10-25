"""Read-job answer validation primitives (completed with citation checking in Task 14)."""

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minItems", "maxItems", "maxLength", "minimum", "maximum", "description",
})
