"""Strict, side-effect-free JSON Schema validation for MCP contracts."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from ai_agent.errors import ProtocolValidationError


def validate_schema(schema: dict[str, Any], *, label: str) -> None:
    """Reject malformed MCP schemas before they can enter the Tool Catalog."""

    _reject_remote_references(schema, label=label)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ProtocolValidationError(f"{label} is not a valid JSON Schema.") from exc


def validate_instance(value: Any, schema: dict[str, Any], *, label: str) -> None:
    """Validate one MCP input or output without resolving remote references."""

    if not schema:
        return
    validate_schema(schema, label=label)
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path)
        location = f" at $.{path}" if path else " at $"
        raise ProtocolValidationError(f"{label} validation failed{location}.") from exc


def _reject_remote_references(value: Any, *, label: str) -> None:
    if not isinstance(value, dict):
        return
    for keyword in ("$ref", "$dynamicRef"):
        reference = value.get(keyword)
        if isinstance(reference, str) and not reference.startswith("#"):
            raise ProtocolValidationError(
                f"{label} must use only local JSON Schema references."
            )
    for keyword in (
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    ):
        _reject_remote_references(value.get(keyword), label=label)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        subschemas = value.get(keyword)
        if isinstance(subschemas, list):
            for subschema in subschemas:
                _reject_remote_references(subschema, label=label)
    for keyword in ("$defs", "definitions", "dependentSchemas", "patternProperties", "properties"):
        subschemas = value.get(keyword)
        if isinstance(subschemas, dict):
            for subschema in subschemas.values():
                _reject_remote_references(subschema, label=label)
