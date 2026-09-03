# Leadpoet Codex Harness

An open-source, live bakeoff for B2B lead-sourcing agent harnesses.

The runner gives the same ICP, model, prompt, provider tools, and limits to five
harnesses:

- PydanticAI
- Pi
- OpenAI Agents SDK
- Codex SDK
- smolagents

Each arm must find up to five companies that fit the ICP and have a verified,
recent intent signal. Results must include clear sales-facing reasons and live
evidence URLs. The winner is the arm with the best blind-reviewed
Sales-Ready@5 result.

This repository does not change Leadpoet production or the current daily
rebenchmark.

## Stable contract

Every bundle exposes:

```python
def run_icp(icp: dict) -> list[dict]:
    """Return up to five best-fit companies, ranked best first."""
```

The normal output shape is:

```json
{
  "company_name": "Example",
  "company_website": "https://example.com/",
  "company_linkedin": "https://www.linkedin.com/company/example/",
  "industry": "Software",
  "employee_count": "51-200",
  "country": "United States",
  "state": "California",
  "fit_summary": "Why the company fits the ICP.",
  "fit_evidence_urls": ["https://example.com/about"],
  "intent_signals": [{
    "matched_icp_signal": 0,
    "description": "The required recent event.",
    "date": "2026-08-20",
    "why_now": "Why a sales representative should contact the company now.",
    "url": "https://example.com/news/event",
    "snippet": "Source text that supports the claim."
  }]
}
```

The host supplies these common tools as plain JSON: `search_companies`,
`get_company_profile`, `get_company_events`, `search_web`, `fetch_page`, and
`submit_companies`.

## Install

Use Python 3.11 or newer and Node.js 22.19 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt \
  -r experiments/harness_bakeoff/adapters/requirements-pydantic-ai.txt \
  -r experiments/harness_bakeoff/adapters/requirements-openai-agents.txt \
  -r experiments/harness_bakeoff/adapters/requirements-smolagents.txt
npm ci --prefix experiments/harness_bakeoff/deepline
npm ci --prefix experiments/harness_bakeoff/pi
npm ci --prefix experiments/harness_bakeoff/codex
export BAKEOFF_DEEPLINE_BIN="$PWD/experiments/harness_bakeoff/deepline/node_modules/.bin/deepline"
```

Set `OPENROUTER_API_KEY`, `DEEPLINE_API_KEY`, and `SCRAPINGDOG_API_KEY` in the
process environment. `EXA_API_KEY` is optional. Do not commit keys or private
ICP data.

## Run the live bakeoff

Store five real ICPs in an uncommitted JSON file outside this repository. The
file can contain a JSON array or an object with an `icps` array.

```bash
python -m experiments.harness_bakeoff.runner preflight
python -m experiments.harness_bakeoff.runner all \
  --icp-file /absolute/path/to/icps.json \
  --evaluation-date YYYY-MM-DD
```

The smoke phase runs each arm once. The scored phase runs five ICPs twice for
50 live attempts. Every attempt uses a fresh process and has the same provider,
token, time, and cost limits. At the default $4 limit per attempt, the scored
matrix has a $200 maximum. Results must be written outside the repository. The
runner supports Linux and macOS.

Use the blind evaluator after the scored run:

```bash
python -m experiments.harness_bakeoff.evaluate packet \
  --results /path/to/results/scored.jsonl \
  --output /path/to/audit \
  --evaluation-date YYYY-MM-DD \
  --expected-icp-id ICP_1 \
  --expected-icp-id ICP_2 \
  --expected-icp-id ICP_3 \
  --expected-icp-id ICP_4 \
  --expected-icp-id ICP_5
```

See [the bakeoff package notes](experiments/harness_bakeoff/README.md) for the
review and tie-break commands.

## Current status

Earlier internal live tests put PydanticAI ahead of Pi, but integration failures
made that result provisional. The standalone five-arm matrix must complete
before one harness is selected as the open-source baseline.

## License

MIT
