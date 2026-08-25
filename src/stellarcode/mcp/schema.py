"""Validate MCP tool schemas before exposing them as provider Function Calling tools."""

from __future__ import annotations

import json
from typing import Any


_DROP_KEYS = frozenset({"$schema", "$id", "$ref", "$defs", "definitions"})
_COPY_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "format",
    }
)


def sanitize_input_schema(schema: object) -> dict[str, Any]:
    cleaned = _clean_schema(schema, top_level=True)
    cleaned["type"] = "object"
    properties = cleaned.get("properties")
    cleaned["properties"] = properties if isinstance(properties, dict) else {}
    required = cleaned.get("required")
    if not isinstance(required, list):
        cleaned.pop("required", None)
    return cleaned


def _clean_schema(schema: object, *, top_level: bool = False) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}} if top_level else {}

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _DROP_KEYS or key in {"anyOf", "oneOf", "allOf"}:
            continue
        if key in _COPY_KEYS:
            cleaned[key] = value
        elif key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                str(name): _clean_schema(child)
                for name, child in value.items()
                if isinstance(name, str)
            }
        elif key == "items":
            cleaned[key] = _clean_schema(value)
        elif key == "required" and isinstance(value, list):
            cleaned[key] = [item for item in value if isinstance(item, str)]
        elif key == "additionalProperties" and isinstance(value, bool):
            cleaned[key] = value

    alternatives = []
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list) and value:
            alternatives.append(
                f"{key} options: "
                + ", ".join(_describe_alternative(item) for item in value[:8])
            )
    if alternatives:
        current = str(cleaned.get("description") or "").strip()
        cleaned["description"] = " ".join(filter(None, (current, *alternatives)))[:1000]
    elif "description" in cleaned:
        cleaned["description"] = str(cleaned["description"])[:1000]

    if "type" not in cleaned:
        cleaned["type"] = "object" if "properties" in cleaned or top_level else "string"
    if cleaned.get("type") == "object" and "properties" not in cleaned:
        cleaned["properties"] = {}
    return cleaned


def _describe_alternative(value: object) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)[:160]
    kind = value.get("type")
    description = value.get("description")
    if kind and description:
        return f"{kind} ({description})"[:160]
    if kind:
        return str(kind)[:160]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:160]
