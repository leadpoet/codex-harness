"""One small, framework-neutral contract for the shared sourcing tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


TOOL_DESCRIPTIONS = {
    "search_companies": (
        "Discover candidate companies with Deepline. Use focused queries and ICP filters."
    ),
    "get_company_profile": "Get Deepline firmographic data for one company domain.",
    "get_company_events": (
        "Find live company events such as jobs or financing for one domain."
    ),
    "search_web": "Search the public web, recent news, or jobs through approved host providers.",
    "fetch_page": (
        "Fetch readable text from a public evidence URL to verify a fit or intent claim."
    ),
    "submit_companies": (
        "Submit the final ranked companies exactly once. This is the terminal sourcing action."
    ),
}


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_companies": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "industry": {"type": "string", "minLength": 1},
            "geography": {"type": "string", "minLength": 1},
            "employee_count": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 20,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 6},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "get_company_profile": {
        "type": "object",
        "properties": {"domain": {"type": "string", "minLength": 1}},
        "required": ["domain"],
        "additionalProperties": False,
    },
    "get_company_events": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "minLength": 1},
            "categories": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 20,
            },
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["domain"],
        "additionalProperties": False,
    },
    "search_web": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": ["search", "news", "jobs"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            "recency_days": {"type": "integer", "minimum": 1, "maximum": 3650},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "fetch_page": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 8},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 4000},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


def tool_input_schema(name: str) -> dict[str, Any]:
    """Return an isolated copy so framework adapters cannot mutate the contract."""

    try:
        return deepcopy(_INPUT_SCHEMAS[name])
    except KeyError as exc:
        raise ValueError(f"no shared input schema for tool {name!r}") from exc


def smol_tool_inputs(name: str) -> dict[str, dict[str, Any]]:
    """Translate the common object schema to smolagents' native input mapping."""

    schema = tool_input_schema(name)
    required = set(schema["required"])
    properties = schema["properties"]
    for parameter, value in properties.items():
        if parameter not in required:
            value["nullable"] = True
    return properties


__all__ = ["TOOL_DESCRIPTIONS", "smol_tool_inputs", "tool_input_schema"]
