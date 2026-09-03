# Codex SDK bakeoff arm

Install this arm without changing production dependencies:

```sh
npm ci --prefix experiments/harness_bakeoff/codex
```

The worker uses the pinned official TypeScript Codex SDK. Each attempt gets a
new empty read-only workspace and a disposable `CODEX_HOME`. Codex uses
OpenRouter's Responses endpoint. Shell, web search, browser, file editing,
plugins, apps, memory, and subagents are disabled. A local stdio MCP bridge is
the only tool surface and forwards the six common sourcing tools to the
attempt-local host boundary.

No provider credential is written to disk or placed in a command argument.
The OpenRouter key and local tool token exist only in process environments.
