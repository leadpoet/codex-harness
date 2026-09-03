"""smolagents challenger for the live sourcing harness bakeoff."""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

from smolagents import LogLevel, OpenAIModel, ToolCallingAgent, tool

from experiments.harness_bakeoff.models import (
    company_list_json_schema,
    validate_companies,
)
from experiments.harness_bakeoff.prompt import SYSTEM_PROMPT, build_prompt
from experiments.harness_bakeoff.tool_client import ToolClient
from experiments.harness_bakeoff.tool_contract import (
    TOOL_DESCRIPTIONS,
    smol_tool_inputs,
)


DEFAULT_MODEL = "openai/gpt-5.6-sol"
LAST_USAGE: dict[str, Any] = {}
_MISSING = object()


def _member(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name, _MISSING)
    try:
        return getattr(value, name)
    except (AttributeError, TypeError):
        return _MISSING


def _token(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_usage(raw_response: Any) -> tuple[dict[str, Any] | None, str]:
    """Read one OpenRouter usage record retained by smolagents' ChatMessage.raw."""

    raw_usage = _member(raw_response, "usage")
    if raw_usage is _MISSING or raw_usage is None:
        return None, "raw response has no usage"
    input_tokens = _token(_member(raw_usage, "prompt_tokens"))
    output_tokens = _token(_member(raw_usage, "completion_tokens"))
    total_tokens = _token(_member(raw_usage, "total_tokens"))
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None, "raw response has invalid token totals"
    if total_tokens != input_tokens + output_tokens:
        return None, "raw response token total is inconsistent"

    entry: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = _member(raw_usage, "prompt_tokens_details")
    cache_read = (
        None
        if prompt_details is _MISSING or prompt_details is None
        else _token(_member(prompt_details, "cached_tokens"))
    )
    cache_write = (
        None
        if prompt_details is _MISSING or prompt_details is None
        else _token(_member(prompt_details, "cache_write_tokens"))
    )
    known_prompt_details: dict[str, int] = {}
    if cache_read is not None:
        known_prompt_details["cached_tokens"] = cache_read
    if cache_write is not None:
        known_prompt_details["cache_write_tokens"] = cache_write
    if known_prompt_details:
        entry["input_tokens_details"] = known_prompt_details

    completion_details = _member(raw_usage, "completion_tokens_details")
    reasoning_tokens = (
        None
        if completion_details is _MISSING or completion_details is None
        else _token(_member(completion_details, "reasoning_tokens"))
    )
    if reasoning_tokens is not None:
        if reasoning_tokens > output_tokens:
            return entry, "raw response reasoning tokens exceed output tokens"
        entry["output_tokens_details"] = {"reasoning_tokens": reasoning_tokens}

    if cache_read is None or cache_write is None:
        return entry, "raw response has incomplete cache token details"
    if cache_read + cache_write > input_tokens:
        return entry, "raw response cache tokens exceed input tokens"
    return entry, ""


def _usage_from_retained_responses(
    *, raw_responses: list[Any], aggregate_usage: Any
) -> dict[str, Any]:
    """Export exact request and aggregate usage, or withhold cost-bearing input."""

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_response in enumerate(raw_responses, start=1):
        entry, error = _request_usage(raw_response)
        if entry is not None:
            entries.append(entry)
        if error:
            errors.append(f"request {index}: {error}")

    raw_input = sum(entry["input_tokens"] for entry in entries)
    raw_output = sum(entry["output_tokens"] for entry in entries)
    raw_total = sum(entry["total_tokens"] for entry in entries)
    framework: dict[str, Any] = {}
    if isinstance(aggregate_usage, dict):
        framework = dict(aggregate_usage)
    elif aggregate_usage is not None:
        try:
            framework = dataclasses.asdict(aggregate_usage)
        except (TypeError, ValueError):
            errors.append("framework aggregate token usage is invalid")
    framework_input = _token(framework.get("input_tokens"))
    framework_output = _token(framework.get("output_tokens"))
    framework_total = _token(framework.get("total_tokens"))
    if framework_input is None or framework_output is None or framework_total is None:
        errors.append("framework aggregate token usage is unavailable")
    elif (
        framework_input != raw_input
        or framework_output != raw_output
        or framework_total != raw_total
    ):
        errors.append(
            "retained response usage does not match framework aggregate usage"
        )
    if not raw_responses:
        errors.append("framework retained no model response steps")
    elif len(entries) != len(raw_responses):
        errors.append("one or more model requests have no readable raw usage")

    input_for_limit = max(
        value for value in (framework_input, raw_input) if value is not None
    )
    output_for_limit = max(
        value for value in (framework_output, raw_output) if value is not None
    )
    total_for_record = max(
        value for value in (framework_total, raw_total) if value is not None
    )
    usage: dict[str, Any] = {
        "aggregate_input_tokens": input_for_limit,
        "output_tokens": output_for_limit,
        "total_tokens": total_for_record,
        "requests": len(raw_responses),
        "request_usage_entries": entries,
        "cache_accounting_complete": not errors,
    }
    if errors:
        # The central estimator deliberately does not recognize
        # aggregate_input_tokens as a billable input field. Omitting
        # input_tokens makes model cost unknown while token-limit enforcement
        # can still use the conservative aggregate above.
        usage["usage_accounting_error"] = "; ".join(dict.fromkeys(errors))
        return usage

    cache_read = sum(
        entry["input_tokens_details"]["cached_tokens"] for entry in entries
    )
    cache_write = sum(
        entry["input_tokens_details"]["cache_write_tokens"] for entry in entries
    )
    usage.update(
        {
            "input_tokens": raw_input,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "input_tokens_details": {
                "cached_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
        }
    )
    reasoning = [
        _member(entry.get("output_tokens_details"), "reasoning_tokens")
        for entry in entries
    ]
    if reasoning and all(_token(value) is not None for value in reasoning):
        usage["output_tokens_details"] = {
            "reasoning_tokens": sum(int(value) for value in reasoning)
        }
    return usage


class _UsageOpenAIModel(OpenAIModel):
    """Retain each successful raw response, including max-step finalization."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retained_raw_responses: list[Any] = []

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        message = super().generate(*args, **kwargs)
        raw_response = _member(message, "raw")
        if raw_response is not _MISSING and raw_response is not None:
            self.retained_raw_responses.append(raw_response)
        return message


def _install_company_list_schema(tool_value: Any) -> None:
    generated = tool_value.inputs.get("companies")
    description = (
        generated.get("description", "") if isinstance(generated, dict) else ""
    )
    schema = company_list_json_schema()
    schema["description"] = description or "Final ranked companies."
    tool_value.inputs["companies"] = schema


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


class _SubmitCompaniesAgent(ToolCallingAgent):
    """Use the shared submit tool as smolagents' terminal tool."""

    def _setup_tools(self, tools: list[Any], add_base_tools: bool) -> None:
        super()._setup_tools(tools, add_base_tools)
        self.tools.pop("final_answer", None)

    def process_tool_calls(self, chat_message: Any, memory_step: Any) -> Any:
        for output in super().process_tool_calls(chat_message, memory_step):
            tool_call = getattr(output, "tool_call", None)
            if getattr(tool_call, "name", None) == "submit_companies":
                output.is_final_answer = True
            yield output


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through a fresh smolagents ToolCallingAgent."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    api_key = _required_environment("OPENROUTER_API_KEY")
    model_name = os.environ.get("BAKEOFF_OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    if not model_name:
        raise RuntimeError("BAKEOFF_OPENROUTER_MODEL cannot be empty")

    max_companies = _positive_integer("BAKEOFF_MAX_COMPANIES", 5, 5)
    max_provider_calls = _positive_integer("BAKEOFF_MAX_PROVIDER_CALLS", 30, 100)
    tool_timeout = _positive_float("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90.0, 600.0)
    budget = _ToolBudget(ToolClient(timeout=tool_timeout), max_provider_calls)
    submitted: dict[str, list[dict[str, Any]]] = {}

    def remote_json(name: str, arguments: dict[str, Any]) -> str:
        result = budget.call(name, arguments)
        return json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), default=str
        )

    @tool
    def search_companies(
        query: str,
        industry: str = "",
        geography: str = "",
        employee_count: list[str] = [],
        limit: int = 5,
    ) -> str:
        """Discover candidate companies with Deepline and the supplied ICP filters.

        Args:
            query: Focused company discovery query.
            industry: Target industry, when known.
            geography: Target geography, when known.
            employee_count: Accepted employee-count bands.
            limit: Maximum candidate rows to return.
        """

        return remote_json(
            "search_companies",
            {
                "query": query,
                "industry": industry,
                "geography": geography,
                "employee_count": employee_count,
                "limit": limit,
            },
        )

    @tool
    def get_company_profile(domain: str) -> str:
        """Get Deepline firmographic data for one company domain.

        Args:
            domain: Company website domain without a path.
        """

        return remote_json("get_company_profile", {"domain": domain})

    @tool
    def get_company_events(
        domain: str,
        categories: list[str] = [],
        query: str = "",
        limit: int = 5,
    ) -> str:
        """Find live company events such as jobs or financing for one domain.

        Args:
            domain: Company website domain without a path.
            categories: Intent categories to search.
            query: Optional event-specific query.
            limit: Maximum event rows to return.
        """

        return remote_json(
            "get_company_events",
            {
                "domain": domain,
                "categories": categories,
                "query": query,
                "limit": limit,
            },
        )

    @tool
    def search_web(
        query: str,
        mode: str = "search",
        limit: int = 5,
        recency_days: int | None = None,
    ) -> str:
        """Search the public web, news, or jobs for evidence.

        Args:
            query: Search query.
            mode: One of search, news, or jobs.
            limit: Maximum search rows to return.
            recency_days: Optional maximum result age in days.
        """

        return remote_json(
            "search_web",
            {
                "query": query,
                "mode": mode,
                "limit": limit,
                "recency_days": recency_days,
            },
        )

    @tool
    def fetch_page(url: str, max_chars: int = 4000) -> str:
        """Fetch one public evidence page and return its extracted text.

        Args:
            url: Absolute public HTTP or HTTPS URL.
            max_chars: Maximum extracted characters to return.
        """

        return remote_json("fetch_page", {"url": url, "max_chars": max_chars})

    @tool
    def submit_companies(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Submit the final ranked companies and end the run.

        Args:
            companies: Final companies matching the schema in the task.
        """

        normalized = validate_companies(companies, max_companies)
        budget.call("submit_companies", {"companies": normalized})
        submitted["companies"] = normalized
        return normalized

    common_tools = [
        search_companies,
        get_company_profile,
        get_company_events,
        search_web,
        fetch_page,
    ]
    for tool_value in common_tools:
        tool_value.description = TOOL_DESCRIPTIONS[tool_value.name]
        tool_value.inputs = smol_tool_inputs(tool_value.name)
    submit_companies.description = TOOL_DESCRIPTIONS["submit_companies"]

    _install_company_list_schema(submit_companies)

    def final_answer_is_valid(answer: Any, _memory: Any, agent: Any) -> bool:
        del agent
        validate_companies(answer, max_companies)
        return True

    model = _UsageOpenAIModel(
        model_id=model_name,
        api_base="https://openrouter.ai/api/v1",
        api_key=api_key,
        client_kwargs={"timeout": 120, "max_retries": 1},
        max_tokens=15_000,
        reasoning_effort="medium",
        extra_body={"usage": {"include": True}},
    )
    agent = _SubmitCompaniesAgent(
        tools=[
            *common_tools,
            submit_companies,
        ],
        model=model,
        instructions=SYSTEM_PROMPT,
        max_steps=30,
        max_tool_threads=1,
        add_base_tools=False,
        final_answer_checks=[final_answer_is_valid],
        return_full_result=True,
        verbosity_level=LogLevel.OFF,
    )
    try:
        result = agent.run(
            build_prompt(icp, max_companies=max_companies),
            reset=True,
            return_full_result=True,
        )
    except Exception:
        usage = _usage_from_retained_responses(
            raw_responses=model.retained_raw_responses,
            aggregate_usage=agent.monitor.get_total_token_counts(),
        )
        usage["provider_calls"] = budget.calls
        LAST_USAGE.clear()
        LAST_USAGE.update(usage)
        raise

    usage = _usage_from_retained_responses(
        raw_responses=model.retained_raw_responses,
        aggregate_usage=result.token_usage,
    )
    usage["provider_calls"] = budget.calls
    usage["duration_seconds"] = result.timing.duration
    LAST_USAGE.clear()
    LAST_USAGE.update(usage)

    companies = submitted.get("companies")
    if result.state != "success" or companies is None:
        raise RuntimeError("agent stopped without a valid submit_companies call")
    companies = validate_companies(result.output, max_companies)

    return companies


__all__ = ["LAST_USAGE", "run_icp"]
