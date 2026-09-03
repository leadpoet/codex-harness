import readline from "node:readline";

const MAX_MESSAGE_BYTES = 1_000_000;
const ALLOWED_TOOLS = new Set([
  "search_companies",
  "get_company_profile",
  "get_company_events",
  "search_web",
  "fetch_page",
  "submit_companies",
]);

const TOOL_DEFINITIONS = [
  {
    name: "search_companies",
    description: "Discover candidate companies with Deepline. Use focused queries and ICP filters.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1 },
        industry: { type: "string", minLength: 1 },
        geography: { type: "string", minLength: 1 },
        employee_count: {
          type: "array",
          items: { type: "string", minLength: 1 },
          maxItems: 20,
        },
        limit: { type: "integer", minimum: 1, maximum: 6 },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "get_company_profile",
    description: "Get Deepline firmographic data for one company domain.",
    inputSchema: {
      type: "object",
      properties: { domain: { type: "string", minLength: 1 } },
      required: ["domain"],
      additionalProperties: false,
    },
  },
  {
    name: "get_company_events",
    description: "Find live company events such as jobs or financing for one domain.",
    inputSchema: {
      type: "object",
      properties: {
        domain: { type: "string", minLength: 1 },
        categories: {
          type: "array",
          items: { type: "string", minLength: 1 },
          maxItems: 20,
        },
        query: { type: "string", minLength: 1 },
        limit: { type: "integer", minimum: 1, maximum: 5 },
      },
      required: ["domain"],
      additionalProperties: false,
    },
  },
  {
    name: "search_web",
    description: "Search the public web, recent news, or jobs through ScrapingDog.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1 },
        mode: { type: "string", enum: ["search", "news", "jobs"] },
        limit: { type: "integer", minimum: 1, maximum: 5 },
        recency_days: { type: "integer", minimum: 1, maximum: 3650 },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "fetch_page",
    description: "Fetch readable text from a public evidence URL to verify a fit or intent claim.",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", minLength: 8 },
        max_chars: { type: "integer", minimum: 1000, maximum: 4000 },
      },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "submit_companies",
    description: "Submit the final ranked companies exactly once. This is the terminal sourcing action.",
    inputSchema: {
      type: "object",
      properties: {
        companies: {
          type: "array",
          items: {
            type: "object",
            properties: {
              company_name: { type: "string", minLength: 1 },
              company_website: { type: "string", minLength: 8 },
              company_linkedin: { type: "string" },
              industry: { type: "string" },
              employee_count: { type: "string" },
              country: { type: "string" },
              state: { type: "string" },
              fit_summary: { type: "string", minLength: 1 },
              fit_evidence_urls: {
                type: "array",
                items: { type: "string", minLength: 8 },
              },
              intent_signals: {
                type: "array",
                minItems: 1,
                items: {
                  type: "object",
                  properties: {
                    matched_icp_signal: { type: "integer", minimum: 0 },
                    description: { type: "string", minLength: 1 },
                    date: { type: "string", format: "date", pattern: "^\\d{4}-\\d{2}-\\d{2}$" },
                    why_now: { type: "string", minLength: 1 },
                    url: { type: "string", minLength: 8 },
                    snippet: { type: "string", minLength: 1 },
                  },
                  required: [
                    "matched_icp_signal",
                    "description",
                    "date",
                    "why_now",
                    "url",
                    "snippet"
                  ],
                  additionalProperties: false,
                },
              },
            },
            required: [
              "company_name",
              "company_website",
              "company_linkedin",
              "industry",
              "employee_count",
              "country",
              "state",
              "fit_summary",
              "fit_evidence_urls",
              "intent_signals"
            ],
            additionalProperties: false,
          },
        },
      },
      required: ["companies"],
      additionalProperties: false,
    },
  },
];

function requiredEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function positiveInteger(name, fallback, maximum) {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    throw new Error(`${name} must be an integer from 1 through ${maximum}`);
  }
  return value;
}

function toolEndpoint(raw) {
  const endpoint = new URL(raw);
  if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
    throw new Error("BAKEOFF_TOOL_URL must use HTTP or HTTPS");
  }
  if (!new Set(["127.0.0.1", "::1", "localhost"]).has(endpoint.hostname.toLowerCase())) {
    throw new Error("BAKEOFF_TOOL_URL must be an attempt-local loopback URL");
  }
  endpoint.pathname = `${endpoint.pathname.replace(/\/$/u, "")}/tool`;
  endpoint.search = "";
  endpoint.hash = "";
  return endpoint.toString();
}

const endpoint = toolEndpoint(requiredEnvironment("BAKEOFF_TOOL_URL"));
const token = requiredEnvironment("BAKEOFF_TOOL_TOKEN");
const maxProviderCalls = positiveInteger("BAKEOFF_MAX_PROVIDER_CALLS", 30, 100);
const timeoutMs = positiveInteger("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90, 600) * 1000;
let providerCalls = 0;
let submitted = false;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function jsonRpcError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

async function callHost(name, argumentsValue) {
  if (!ALLOWED_TOOLS.has(name)) throw new Error(`Unknown sourcing tool: ${name}`);
  if (submitted) throw new Error("submit_companies already terminated provider access");
  if (name !== "submit_companies") {
    providerCalls += 1;
    if (providerCalls > maxProviderCalls) {
      throw new Error(`Provider-call limit of ${maxProviderCalls} exceeded`);
    }
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      accept: "application/json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ name, arguments: argumentsValue }),
    redirect: "error",
    signal: AbortSignal.timeout(timeoutMs),
  });
  const raw = await response.text();
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new Error(`${name} returned non-JSON with HTTP ${response.status}`);
  }
  if (!response.ok || payload?.ok !== true) {
    const detail = typeof payload?.error === "string" ? payload.error : `HTTP ${response.status}`;
    throw new Error(`${name} failed: ${detail}`);
  }
  if (name === "submit_companies") submitted = true;
  return payload.result;
}

async function handle(request) {
  if (!request || request.jsonrpc !== "2.0" || typeof request.method !== "string") {
    jsonRpcError(request?.id ?? null, -32600, "Invalid Request");
    return;
  }
  const { id, method, params = {} } = request;

  if (method === "initialize") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params.protocolVersion || "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "leadpoet-sourcing", version: "0.1.0" },
      },
    });
    return;
  }
  if (method === "notifications/initialized" || method === "notifications/cancelled") return;
  if (method === "ping") {
    send({ jsonrpc: "2.0", id, result: {} });
    return;
  }
  if (method === "tools/list") {
    send({ jsonrpc: "2.0", id, result: { tools: TOOL_DEFINITIONS } });
    return;
  }
  if (method === "resources/list" || method === "prompts/list") {
    const field = method.startsWith("resources/") ? "resources" : "prompts";
    send({ jsonrpc: "2.0", id, result: { [field]: [] } });
    return;
  }
  if (method !== "tools/call") {
    jsonRpcError(id ?? null, -32601, "Method not found");
    return;
  }

  const name = typeof params.name === "string" ? params.name : "";
  const argumentsValue =
    params.arguments && !Array.isArray(params.arguments) && typeof params.arguments === "object"
      ? params.arguments
      : {};
  try {
    const result = await callHost(name, argumentsValue);
    const text = name === "submit_companies" ? "Submission accepted. The run is complete." : JSON.stringify(result);
    send({
      jsonrpc: "2.0",
      id,
      result: {
        content: [{ type: "text", text }],
        isError: false,
      },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    send({
      jsonrpc: "2.0",
      id,
      result: {
        content: [{ type: "text", text: JSON.stringify({ ok: false, error: message }) }],
        isError: true,
      },
    });
  }
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let queue = Promise.resolve();
lines.on("line", (line) => {
  if (Buffer.byteLength(line, "utf8") > MAX_MESSAGE_BYTES) {
    jsonRpcError(null, -32600, "Request too large");
    return;
  }
  queue = queue.then(async () => {
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      jsonRpcError(null, -32700, "Parse error");
      return;
    }
    await handle(request);
  }).catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`Sourcing MCP bridge error: ${message}\n`);
  });
});
