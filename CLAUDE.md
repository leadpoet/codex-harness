# Leadpoet Codex Harness agent guide

`AGENTS.md` and `CLAUDE.md` must remain byte-identical.

## Purpose

This repository compares five open-source agent harnesses on live B2B company
sourcing. Keep it independent from the production Research Lab and its current
private sourcing model.

## Simple competition boundary

The fixed contract is only `run_icp(icp) -> list[dict]`, the documented input
and output shapes, host-supplied approved APIs, and external time and cost
limits. Competitors can change the model, harness, prompts, routing, tools, and
dependencies.

Do not add Git or GitHub attestation, commit or corpus hashes, manifests,
receipts, replay proofs, or compatibility layers. Do not add production
deployment, autoresearch, miner, validator, or chain code.

## Data and tests

Never commit credentials, private ICP payloads, provider responses, or customer
data. Credentials must come from environment variables. Live scored tests must
use real ICPs and real provider calls; do not replace them with mocks. Keep
deterministic unit tests small and focused.
