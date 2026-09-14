# Leadpoet Codex Harness

An open-source Codex SDK harness for live B2B company sourcing.

It accepts an ICP, searches approved provider APIs, and returns up to five
companies with verified recent intent, clear sales-facing reasons, and public
evidence URLs. This repository contains only the Codex SDK implementation.

The internal live evaluation produced 2 reviewed sales-ready companies in 10
attempts. PydanticAI won that bakeoff, so this repository is a successful
alternative and not the current winning baseline.

This repository does not change Leadpoet production or the daily rebenchmark.

## Stable entrypoint

```python
from harness import run_icp

companies = run_icp(icp)
```

The callable contract is:

```python
def run_icp(icp: dict) -> list[dict]:
    """Return up to five best-fit companies, ranked best first."""
```

Each company includes its identity and firmographics, a fit summary, fit
evidence URLs, and dated intent signals with a plain-language `why_now` and a
supporting URL.

The host supplies six JSON tools: `search_companies`, `get_company_profile`,
`get_company_events`, `search_web`, `fetch_page`, and `submit_companies`.

## Install

Use Python 3.11 or newer and Node.js 20 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix experiments/harness_bakeoff/deepline
npm ci --prefix experiments/harness_bakeoff/codex
export BAKEOFF_DEEPLINE_BIN="$PWD/experiments/harness_bakeoff/deepline/node_modules/.bin/deepline"
```

Set `OPENROUTER_API_KEY`, `DEEPLINE_API_KEY`, and `SCRAPINGDOG_API_KEY` in the
process environment. `EXA_API_KEY` is optional. Never commit keys or private ICP
data.

## Run

Keep real ICPs in an uncommitted JSON file outside this repository.

```bash
python -m experiments.harness_bakeoff.runner preflight
python -m experiments.harness_bakeoff.runner all \
  --icp-file /absolute/path/to/icps.json \
  --evaluation-date YYYY-MM-DD
```

The smoke phase runs once. The scored phase runs each selected ICP twice with a
fresh process and the documented provider, token, time, and cost limits.

## License

Copyright (c) 2026 Leadpoet.

Licensed under the GNU Affero General Public License, version 3 only
(`AGPL-3.0-only`). See [LICENSE](LICENSE).
