"""OpenAI Agents SDK challenger for the live sourcing harness bakeoff."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunConfig,
    RunContextWrapper,
    Runner,
    StopAtTools,
    function_tool,
)
from openai import AsyncOpenAI

from experiments.harness_bakeoff.models import CompanyResult, validate_companies
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
    submitted: dict[str, list[dict[str, Any]]] = {}

    @function_tool(strict_mode=False)
    def search_companies(
        query: str,
        industry: str = "",
        geography: str = "",
        employee_count: list[str] = [],
        limit: int = 5,
    ) -> Any:
        """Discover candidate companies with Deepline and the supplied ICP filters.

        Args:
            query: Focused company discovery query.
            industry: Target industry, when known.
            geography: Target geography, when known.
            employee_count: Accepted employee-count bands.
            limit: Maximum candidate rows to return.
        """

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

    @function_tool(strict_mode=False)
    def get_company_profile(domain: str) -> Any:
        """Get Deepline firmographic data for one company domain.

        Args:
            domain: Company website domain without a path.
        """

        return budget.call("get_company_profile", {"domain": domain})

    @function_tool(strict_mode=False)
    def get_company_events(
        domain: str,
        categories: list[str] = [],
        query: str = "",
        limit: int = 5,
    ) -> Any:
        """Find live company events such as jobs or financing for one domain.

        Args:
            domain: Company website domain without a path.
            categories: Intent categories to search.
            query: Optional event-specific query.
            limit: Maximum event rows to return.
        """

        return budget.call(
            "get_company_events",
            {
                "domain": domain,
                "categories": categories,
                "query": query,
                "limit": limit,
            },
        )

    @function_tool(strict_mode=False)
    def search_web(
        query: str,
        mode: str = "search",
        limit: int = 5,
        recency_days: int | None = None,
    ) -> Any:
        """Search the public web, news, or jobs for evidence.

        Args:
            query: Search query.
            mode: One of search, news, or jobs.
            limit: Maximum search rows to return.
            recency_days: Optional maximum result age in days.
        """

        return budget.call(
            "search_web",
            {
                "query": query,
                "mode": mode,
                "limit": limit,
                "recency_days": recency_days,
            },
        )

    @function_tool(strict_mode=False)
    def fetch_page(url: str, max_chars: int = 4000) -> Any:
        """Fetch one public evidence page and return its extracted text.

        Args:
            url: Absolute public HTTP or HTTPS URL.
            max_chars: Maximum extracted characters to return.
        """

        return budget.call("fetch_page", {"url": url, "max_chars": max_chars})

    @function_tool
    def submit_companies(companies: list[CompanyResult]) -> str:
        """Submit the final ranked companies and end the run.

        Args:
            companies: Final companies that match the declared output schema.
        """

        normalized = validate_companies(
            [company.model_dump(mode="json") for company in companies],
            max_companies,
        )
        budget.call("submit_companies", {"companies": normalized})
        submitted["companies"] = normalized
        return json.dumps({"accepted": len(normalized)}, separators=(",", ":"))

    common_tools = [
        search_companies,
        get_company_profile,
        get_company_events,
        search_web,
        fetch_page,
    ]
    for tool_value in common_tools:
        tool_value.description = TOOL_DESCRIPTIONS[tool_value.name]
        tool_value.params_json_schema = tool_input_schema(tool_value.name)
        tool_value.strict_json_schema = False
    submit_companies.description = TOOL_DESCRIPTIONS["submit_companies"]

    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=120,
        max_retries=1,
    )
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=openai_client)
    agent = Agent(
        name="lead_sourcing_bakeoff",
        instructions=SYSTEM_PROMPT,
        model=model,
        model_settings=ModelSettings(
            max_tokens=15_000,
            reasoning={"effort": "medium"},
            parallel_tool_calls=False,
            tool_choice="required",
            include_usage=True,
            timeout=120,
        ),
        tools=[
            *common_tools,
            submit_companies,
        ],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_companies"]),
    )
    run_context = RunContextWrapper(context=None)

    try:
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                build_prompt(icp, max_companies=max_companies),
                context=run_context,
                max_turns=30,
                run_config=RunConfig(
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                ),
            ),
            timeout=run_timeout,
        )
    finally:
        usage = dataclasses.asdict(run_context.usage)
        LAST_USAGE.clear()
        LAST_USAGE.update(json.loads(json.dumps(usage, default=str)))
        LAST_USAGE["provider_calls"] = budget.calls
        await openai_client.close()

    companies = submitted.get("companies")
    if companies is None:
        raise RuntimeError("agent stopped without calling submit_companies")

    return companies


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through a fresh OpenAI Agents SDK agent."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run(icp))
    raise RuntimeError("run_icp must be called outside an active asyncio event loop")


__all__ = ["LAST_USAGE", "run_icp"]
