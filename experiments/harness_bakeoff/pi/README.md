# Pi bakeoff arm

This isolated Node worker uses `@earendil-works/pi-coding-agent` with an
in-memory session, an overridden sourcing prompt, and no coding tools. The only
active tools are the five bakeoff HTTP tools and the terminal
`submit_companies` tool.

Install this arm without changing production Python dependencies:

```bash
npm ci --prefix experiments/harness_bakeoff/pi
```

The Python adapter starts the worker with one ICP JSON object on standard input.
The host supplies `OPENROUTER_API_KEY`, `BAKEOFF_TOOL_URL`, and
`BAKEOFF_TOOL_TOKEN` in process memory. Optional settings are
`BAKEOFF_OPENROUTER_MODEL` (or the direct-worker alias `BAKEOFF_MODEL`),
`BAKEOFF_SYSTEM_PROMPT`, `BAKEOFF_MAX_COMPANIES`,
`BAKEOFF_MAX_PROVIDER_CALLS`, `BAKEOFF_RUN_TIMEOUT_SECONDS`, and
`BAKEOFF_TOOL_TIMEOUT_SECONDS`. The worker also enforces
`BAKEOFF_MAX_TURNS`, `BAKEOFF_MAX_INPUT_TOKENS`, and
`BAKEOFF_MAX_OUTPUT_TOKENS` when the host supplies them. Pi's input limit and
reported `aggregate_input_tokens` include normal input, cache reads, and cache
writes.
