"""PydanticAI challenger for the live sourcing harness bakeoff."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, Tool, ToolOutput
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import RunUsage, UsageLimits

from experiments.harness_bakeoff.models import CompaniesResult, validate_companies
from experiments.harness_bakeoff.prompt import SYSTEM_PROMPT, build_prompt
from experiments.harness_bakeoff.tool_client import ToolClient
from experiments.harness_bakeoff.tool_contract import (
    TOOL_DESCRIPTIONS,
    tool_input_schema,
)


DEFAULT_MODEL = "openai/gpt-5.6-sol"
LAST_USAGE: dict[str, Any] = {}


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_integer(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be from 1 through {maximum}")
    return value


def _positive_float(name: str, default: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum:g}")
    return value


class _ToolBudget:
    def __init__(self, client: ToolClient, maximum: int) -> None:
        self.client = client
        self.maximum = maximum
        self.calls = 0

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "submit_companies":
            if self.calls >= self.maximum:
                raise RuntimeError(f"provider-call limit of {self.maximum} exceeded")
            self.calls += 1
        try:
            return self.client.call(name, arguments)
        except Exception as exc:
            if name == "submit_companies":
                raise
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


async def _run(icp: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = _required_environment("OPENROUTER_API_KEY")
    model_name = os.environ.get("BAKEOFF_OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    if not model_name:
        raise RuntimeError("BAKEOFF_OPENROUTER_MODEL cannot be empty")

    max_companies = _positive_integer("BAKEOFF_MAX_COMPANIES", 5, 5)
    max_provider_calls = _positive_integer("BAKEOFF_MAX_PROVIDER_CALLS", 30, 100)
    run_timeout = _positive_float("BAKEOFF_RUN_TIMEOUT_SECONDS", 720.0, 3600.0)
    tool_timeout = _positive_float("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90.0, 600.0)
    budget = _ToolBudget(ToolClient(timeout=tool_timeout), max_provider_calls)

    def search_companies(
        query: str,
        industry: str = "",
        geography: str = "",
        employee_count: list[str] = [],
        limit: int = 5,
    ) -> Any:
        """Discover candidate companies with Deepline and the supplied ICP filters."""

        return budget.call(
            "search_companies",
            {
                "query": query,
                "industry": industry,
                "geography": geography,
                "employee_count": employee_count,
                "limit": limit,
            },
        )

    def get_company_profile(domain: str) -> Any:
        """Get Deepline firmographic data for one company domain."""

        return budget.call("get_company_profile", {"domain": domain})

    def get_company_events(
        domain: str,
        categories: list[str] = [],
        query: str = "",
        limit: int = 5,
    ) -> Any:
        """Find live company events such as jobs or financing for one domain."""

        return budget.call(
            "get_company_events",
            {
                "domain": domain,
                "categories": categories,
                "query": query,
                "limit": limit,
            },
        )

    def search_web(
        query: str,
        mode: str = "search",
        limit: int = 5,
        recency_days: int | None = None,
    ) -> Any:
        """Search the public web, news, or jobs for evidence."""

        return budget.call(
            "search_web",
            {
                "query": query,
                "mode": mode,
                "limit": limit,
                "recency_days": recency_days,
            },
        )

    def fetch_page(url: str, max_chars: int = 4000) -> Any:
        """Fetch one public evidence page and return its extracted text."""

        return budget.call("fetch_page", {"url": url, "max_chars": max_chars})

    model_settings: OpenRouterModelSettings = {
        "max_tokens": 15_000,
        "parallel_tool_calls": False,
        "timeout": 120,
        "openrouter_reasoning": {"effort": "medium"},
        "openrouter_usage": {"include": True},
    }
    model = OpenRouterModel(
        model_name,
        provider=OpenRouterProvider(api_key=api_key),
        settings=model_settings,
    )
    agent = Agent(
        model,
        instructions=SYSTEM_PROMPT,
        tools=[
            Tool.from_schema(
                search_companies,
                "search_companies",
                TOOL_DESCRIPTIONS["search_companies"],
                tool_input_schema("search_companies"),
            ),
            Tool.from_schema(
                get_company_profile,
                "get_company_profile",
                TOOL_DESCRIPTIONS["get_company_profile"],
                tool_input_schema("get_company_profile"),
            ),
            Tool.from_schema(
                get_company_events,
                "get_company_events",
                TOOL_DESCRIPTIONS["get_company_events"],
                tool_input_schema("get_company_events"),
            ),
            Tool.from_schema(
                search_web,
                "search_web",
                TOOL_DESCRIPTIONS["search_web"],
                tool_input_schema("search_web"),
            ),
            Tool.from_schema(
                fetch_page,
                "fetch_page",
                TOOL_DESCRIPTIONS["fetch_page"],
                tool_input_schema("fetch_page"),
            ),
        ],
        output_type=ToolOutput(
            CompaniesResult,
            name="submit_companies",
            description=TOOL_DESCRIPTIONS["submit_companies"],
            strict=True,
        ),
        model_settings=model_settings,
        tool_timeout=tool_timeout,
    )
    run_usage = RunUsage()
    try:
        result = await asyncio.wait_for(
            agent.run(
                build_prompt(icp, max_companies=max_companies),
                usage_limits=UsageLimits(
                    cost_limit=Decimal("4"),
                    request_limit=30,
                    tool_calls_limit=30,
                    input_tokens_limit=120_000,
                    output_tokens_limit=15_000,
                ),
                usage=run_usage,
            ),
            timeout=run_timeout,
        )
        companies = validate_companies(
            result.output.model_dump(mode="json"), max_companies
        )
        budget.call("submit_companies", {"companies": companies})
        return companies
    finally:
        usage = dataclasses.asdict(run_usage)
        LAST_USAGE.clear()
        LAST_USAGE.update(json.loads(json.dumps(usage, default=str)))
        LAST_USAGE["provider_calls"] = budget.calls


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through a fresh PydanticAI agent."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run(icp))
    raise RuntimeError("run_icp must be called outside an active asyncio event loop")


__all__ = ["LAST_USAGE", "run_icp"]
