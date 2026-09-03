"""One prompt used unchanged by all challenger harnesses."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from .models import normalize_icp


SYSTEM_PROMPT = """You are a rigorous B2B account researcher. Find companies that fit the supplied ICP and have the REQUIRED recent intent. Use only the provided tools. Never rely on memory for a factual claim. Verify company fit and each intent against public source content, preserve exact source URLs, and reject stale, ambiguous, homepage-only, or wrong-company evidence. Prefer direct company, job, regulatory, filing, or reputable news pages. Return at most the requested number, ranked best first. Explain fit and why-now in plain language useful to a salesperson. Do not invent missing facts. Call submit_companies exactly once when done."""


def build_prompt(icp: dict[str, Any], max_companies: int | None = None) -> str:
    normalized = normalize_icp(icp)
    limit = max(1, min(int(max_companies or 5), 5))
    raw_day = os.environ.get("BAKEOFF_EVALUATION_DATE", "").strip()
    evaluation_date = date.fromisoformat(raw_day) if raw_day else date.today()
    return (
        f"Evaluation date: {evaluation_date.isoformat()}\n"
        f"Return up to {limit} companies. A company without a verified required intent must not be returned.\n"
        "The matched_icp_signal index refers to required_intents in this normalized ICP:\n"
        f"{json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)}\n\n"
        "Use one bounded funnel: start with one search_companies call and shortlist at most "
        f"{min(limit + 1, 6)} domains; repeat discovery only if it returns no usable domains. "
        "Use get_company_profile only when discovery lacks required fit facts. Research only the REQUIRED intent categories. For each shortlisted domain, try get_company_events OR search_web first and use the other only when the first has no usable evidence. Fetch only the best evidence URL, with one alternate after a failed or unsupported page. Never repeat an equivalent query, domain lookup, or URL. Submit as soon as enough companies pass or the remaining budget cannot improve the result. Company homepages may support identity or fit, but cannot alone prove a dated intent event. Submit ordinary JSON matching the declared company schema."
    )
