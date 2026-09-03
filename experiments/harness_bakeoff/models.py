"""Small public input/output contract shared by every bakeoff arm."""

from __future__ import annotations

import ipaddress
from copy import deepcopy
from datetime import date as ISODate
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


def _public_http_url(value: str, *, allow_empty: bool = False) -> str:
    value = str(value or "").strip()
    if not value and allow_empty:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("must be an absolute HTTP(S) URL")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("must be a public URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("must be a public URL")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


class IntentSignal(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    matched_icp_signal: int = Field(ge=0)
    description: str = Field(min_length=1)
    date: ISODate
    why_now: str = Field(min_length=1)
    url: str
    snippet: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _public_http_url(value)


class CompanyResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    company_name: str = Field(min_length=1)
    company_website: str
    company_linkedin: str
    industry: str
    employee_count: str
    country: str
    state: str
    fit_summary: str = Field(min_length=1)
    fit_evidence_urls: list[str]
    intent_signals: list[IntentSignal] = Field(min_length=1)

    @field_validator("company_website")
    @classmethod
    def validate_website(cls, value: str) -> str:
        return _public_http_url(value)

    @field_validator("company_linkedin")
    @classmethod
    def validate_linkedin(cls, value: str) -> str:
        return _public_http_url(value, allow_empty=True)

    @field_validator("fit_evidence_urls")
    @classmethod
    def validate_fit_urls(cls, values: list[str]) -> list[str]:
        return [_public_http_url(value) for value in values]


class CompaniesResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    companies: list[CompanyResult] = Field(default_factory=list)


def company_list_json_schema() -> dict[str, Any]:
    """Return the canonical company-array schema with local references inlined."""
    raw = TypeAdapter(list[CompanyResult]).json_schema()
    definitions = raw.get("$defs") if isinstance(raw.get("$defs"), dict) else {}

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"unresolved schema reference: {reference}")
            merged = {
                **target,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
            return inline(merged)
        return {key: inline(item) for key, item in value.items() if key != "$defs"}

    schema = inline(raw)
    if not isinstance(schema, dict):
        raise RuntimeError("canonical company schema is unavailable")
    return schema


def _employee_bands(value: Any) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else str(value).split("|")
    return [str(item).strip() for item in raw if str(item).strip()]


def _intent_rows(icp: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    explicit = icp.get("required_intents")
    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, dict):
                rows.append(dict(item))
    if not rows:
        texts = icp.get("intent_signals")
        categories = icp.get("intent_signal_evidence_types")
        ages = icp.get("intent_signal_max_age_days")
        if isinstance(texts, list):
            for index, text in enumerate(texts):
                rows.append(
                    {
                        "signal": text,
                        "category": (
                            categories[index]
                            if isinstance(categories, list) and index < len(categories)
                            else icp.get("intent_category") if len(texts) == 1 else ""
                        ),
                        "max_age_days": (
                            ages[index]
                            if isinstance(ages, list) and index < len(ages)
                            else (
                                icp.get("intent_max_age_days")
                                if len(texts) == 1
                                else None
                            )
                        ),
                    }
                )
    if not rows:
        signal = icp.get("intent_signal_text") or icp.get("intent_signal")
        if signal:
            rows.append(
                {
                    "signal": signal,
                    "category": icp.get("intent_category")
                    or icp.get("intent_source")
                    or "",
                    "max_age_days": icp.get("intent_max_age_days"),
                }
            )
    clean: list[dict[str, Any]] = []
    for row in rows:
        signal = str(
            row.get("signal") or row.get("intent_signal") or row.get("text") or ""
        ).strip()
        if not signal:
            continue
        age = row.get("max_age_days", row.get("intent_max_age_days"))
        try:
            age = int(age) if age not in (None, "") else None
        except (TypeError, ValueError):
            age = None
        clean.append(
            {
                "signal": signal,
                "category": str(row.get("category") or row.get("intent_category") or "")
                .strip()
                .upper(),
                "max_age_days": age,
            }
        )
    return clean


def normalize_icp(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize only the inconsistent fields needed by the public runner."""
    if not isinstance(value, dict):
        raise TypeError("icp must be a dictionary")
    icp = deepcopy(value)
    icp["employee_count"] = _employee_bands(icp.get("employee_count"))
    intents = _intent_rows(icp)
    icp["required_intents"] = intents
    if intents:
        icp["intent_signal"] = intents[0]["signal"]
        icp["intent_category"] = intents[0]["category"]
        icp["intent_max_age_days"] = intents[0]["max_age_days"]
    icp["bonus_intents"] = [
        dict(row) for row in icp.get("bonus_intents", []) if isinstance(row, dict)
    ]
    return icp


def validate_companies(
    value: Any, max_companies: int | None = None
) -> list[dict[str, Any]]:
    """Parse ordinary JSON without repairing quality or ranking mistakes."""
    if isinstance(value, dict) and "companies" in value:
        value = value["companies"]
    if not isinstance(value, list):
        raise ValueError("runner output must be a list or {'companies': [...]} object")
    cap = max(0, int(max_companies if max_companies is not None else 5))
    if cap == 0:
        return []
    if len(value) > cap:
        raise ValueError(f"runner output cannot contain more than {cap} companies")
    results: list[dict[str, Any]] = []
    for raw in value:
        company = CompanyResult.model_validate(raw)
        results.append(company.model_dump(mode="json"))
    return results
