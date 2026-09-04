"""Live OpenRouter model and tool-use preflight for this harness."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


PRIMARY_MODEL = "openai/gpt-5.6-sol"
FALLBACK_MODEL = "openai/gpt-5.5"
PRICE_FIELDS = (
    "prompt",
    "completion",
    "request",
    "internal_reasoning",
    "input_cache_read",
    "input_cache_write",
)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/leadpoet/codex-harness",
        "X-Title": "Leadpoet Codex harness",
    }


def available_models(api_key: str) -> dict[str, dict[str, Any]]:
    response = httpx.get(
        "https://openrouter.ai/api/v1/models", headers=_headers(api_key), timeout=30
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return {
        str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id")
    }


def _safe_price(value: Any) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return str(parsed)


def _safe_pricing(model: dict[str, Any]) -> dict[str, Any]:
    """Keep only public numeric prices needed for local run accounting."""
    raw = model.get("pricing")
    if not isinstance(raw, dict):
        return {}
    pricing = {
        key: price
        for key in PRICE_FIELDS
        if (price := _safe_price(raw.get(key))) is not None
    }
    overrides: list[dict[str, Any]] = []
    raw_overrides = raw.get("overrides")
    for value in raw_overrides if isinstance(raw_overrides, list) else []:
        if not isinstance(value, dict):
            continue
        try:
            minimum = int(value["min_prompt_tokens"])
        except (KeyError, TypeError, ValueError):
            continue
        if minimum < 0:
            continue
        override: dict[str, Any] = {"min_prompt_tokens": minimum}
        override.update(
            {
                key: price
                for key in PRICE_FIELDS
                if (price := _safe_price(value.get(key))) is not None
            }
        )
        overrides.append(override)
    if overrides:
        pricing["overrides"] = sorted(
            overrides, key=lambda item: item["min_prompt_tokens"]
        )
    return pricing


def tool_probe(api_key: str, model: str) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Call ping with value exactly 'ok'."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Required test tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "ping"}},
        "reasoning": {"effort": "medium"},
        "usage": {"include": True},
        "max_tokens": 64,
    }
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=_headers(api_key),
        json=body,
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    calls = choices[0].get("message", {}).get("tool_calls", []) if choices else []
    if not calls or calls[0].get("function", {}).get("name") != "ping":
        raise RuntimeError(f"model {model} did not complete the required tool probe")
    usage = payload.get("usage") or {}
    return {
        "model": model,
        "tool_call": True,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cost_usd": usage.get("cost"),
    }


def select_model(api_key: str) -> dict[str, Any]:
    models = available_models(api_key)
    candidates = [model for model in (PRIMARY_MODEL, FALLBACK_MODEL) if model in models]
    errors: list[str] = []
    for model in candidates:
        try:
            return {
                "selected": model,
                "probe": tool_probe(api_key, model),
                "pricing": _safe_pricing(models[model]),
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:300]}")
    if not candidates:
        raise RuntimeError("neither required OpenRouter model is currently listed")
    raise RuntimeError("no required model passed tool use: " + "; ".join(errors))
