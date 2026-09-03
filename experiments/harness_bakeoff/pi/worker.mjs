import {
  InMemoryCredentialStore,
  Type,
} from "@earendil-works/pi-ai";
import {
  createAgentSession,
  DefaultResourceLoader,
  defineTool,
  ModelRuntime,
  resolveCliModel,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";

const DEFAULT_MODEL = "openai/gpt-5.6-sol";
const DEFAULT_TIMEOUT_SECONDS = 720;
const DEFAULT_TOOL_TIMEOUT_SECONDS = 90;
const DEFAULT_MAX_PROVIDER_CALLS = 30;
const DEFAULT_MAX_COMPANIES = 5;
const DEFAULT_MAX_TURNS = 30;
const DEFAULT_MAX_INPUT_TOKENS = 120_000;
const DEFAULT_MAX_OUTPUT_TOKENS = 15_000;

const DEFAULT_SYSTEM_PROMPT = "You are a rigorous B2B account researcher. Find companies that fit the supplied ICP and have the REQUIRED recent intent. Use only the provided tools. Never rely on memory for a factual claim. Verify company fit and each intent against public source content, preserve exact source URLs, and reject stale, ambiguous, homepage-only, or wrong-company evidence. Prefer direct company, job, regulatory, filing, or reputable news pages. Return at most the requested number, ranked best first. Explain fit and why-now in plain language useful to a salesperson. Do not invent missing facts. Call submit_companies exactly once when done.";

function readPositiveInteger(name, fallback, maximum = Number.MAX_SAFE_INTEGER) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed <= 0 || parsed > maximum) {
    throw new Error(`${name} must be an integer from 1 through ${maximum}`);
  }
  return parsed;
}

function requireEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

async function readStandardInput() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > 1_000_000) throw new Error("ICP input exceeds 1 MB");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) throw new Error("Expected one ICP JSON object on stdin");
  const value = JSON.parse(text);
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new Error("ICP input must be a JSON object");
  }
  return value;
}

function normalizeToolBaseUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("BAKEOFF_TOOL_URL must use http or https");
  }
  url.pathname = `${url.pathname.replace(/\/$/u, "")}/tool`;
  url.search = "";
  url.hash = "";
  return url.toString();
}

function validateCompanies(value, maximum) {
  if (!Array.isArray(value)) return "companies must be an array";
  if (value.length > maximum) return `companies cannot contain more than ${maximum} entries`;
  for (const [index, company] of value.entries()) {
    if (!company || Array.isArray(company) || typeof company !== "object") {
      return `companies[${index}] must be an object`;
    }
  }
  return undefined;
}

function textResult(value, details = {}) {
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    details,
  };
}

function errorResult(error) {
  const message = error instanceof Error ? error.message : String(error);
  return {
    content: [{ type: "text", text: JSON.stringify({ ok: false, error: message }) }],
    details: {},
    isError: true,
  };
}

async function main() {
  const input = await readStandardInput();
  const isAdapterRequest = input.protocol === "leadpoet.harness_bakeoff.pi.v1";
  const icp = isAdapterRequest ? input.icp : input;
  if (!icp || Array.isArray(icp) || typeof icp !== "object") {
    throw new Error("ICP input must be a JSON object");
  }
  const openRouterKey = requireEnvironment("OPENROUTER_API_KEY");
  const toolToken = requireEnvironment("BAKEOFF_TOOL_TOKEN");
  const toolEndpoint = normalizeToolBaseUrl(requireEnvironment("BAKEOFF_TOOL_URL"));
  const maxCompanies = readPositiveInteger("BAKEOFF_MAX_COMPANIES", DEFAULT_MAX_COMPANIES, 20);
  const maxProviderCalls = readPositiveInteger(
    "BAKEOFF_MAX_PROVIDER_CALLS",
    DEFAULT_MAX_PROVIDER_CALLS,
    100,
  );
  const maxTurns = readPositiveInteger("BAKEOFF_MAX_TURNS", DEFAULT_MAX_TURNS, 100);
  const maxInputTokens = readPositiveInteger(
    "BAKEOFF_MAX_INPUT_TOKENS",
    DEFAULT_MAX_INPUT_TOKENS,
    2_000_000,
  );
  const maxOutputTokens = readPositiveInteger(
    "BAKEOFF_MAX_OUTPUT_TOKENS",
    DEFAULT_MAX_OUTPUT_TOKENS,
    200_000,
  );
  const timeoutMs = readPositiveInteger("BAKEOFF_RUN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 3600) * 1000;
  const toolTimeoutMs =
    readPositiveInteger("BAKEOFF_TOOL_TIMEOUT_SECONDS", DEFAULT_TOOL_TIMEOUT_SECONDS, 600) * 1000;
  const modelId = (
    process.env.BAKEOFF_OPENROUTER_MODEL?.trim() ||
    process.env.BAKEOFF_MODEL?.trim() ||
    DEFAULT_MODEL
  ).replace(/^openrouter\//u, "");
  const systemPrompt =
    (isAdapterRequest && typeof input.system_prompt === "string" ? input.system_prompt.trim() : "") ||
    process.env.BAKEOFF_SYSTEM_PROMPT?.trim() ||
    DEFAULT_SYSTEM_PROMPT;
  const userPrompt =
    (isAdapterRequest && typeof input.prompt === "string" ? input.prompt.trim() : "") ||
    `Source the best matching companies for this normalized ICP.\n\n${JSON.stringify(icp, null, 2)}`;

  let providerCallCount = 0;
  let submittedCompanies;

  async function invokeRemote(name, argumentsValue, outerSignal) {
    if (name !== "submit_companies") {
      providerCallCount += 1;
      if (providerCallCount > maxProviderCalls) {
        throw new Error(`Provider-call limit of ${maxProviderCalls} exceeded`);
      }
    }

    const timeoutSignal = AbortSignal.timeout(toolTimeoutMs);
    const signal = outerSignal ? AbortSignal.any([outerSignal, timeoutSignal]) : timeoutSignal;
    const response = await fetch(toolEndpoint, {
      method: "POST",
      headers: {
        accept: "application/json",
        authorization: `Bearer ${toolToken}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ name, arguments: argumentsValue }),
      redirect: "error",
      signal,
    });

    const raw = await response.text();
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      throw new Error(`${name} returned a non-JSON response with status ${response.status}`);
    }
    if (!response.ok || payload?.ok !== true) {
      const detail = typeof payload?.error === "string" ? payload.error : `HTTP ${response.status}`;
      throw new Error(`${name} failed: ${detail}`);
    }
    return payload.result;
  }

  function remoteTool({ name, label, description, parameters }) {
    return defineTool({
      name,
      label,
      description,
      parameters,
      executionMode: "sequential",
      async execute(_toolCallId, params, signal) {
        try {
          const result = await invokeRemote(name, params, signal);
          return textResult(result, { providerCallCount });
        } catch (error) {
          return errorResult(error);
        }
      },
    });
  }

  const tools = [
    remoteTool({
      name: "search_companies",
      label: "Search companies",
      description: "Discover candidate companies with Deepline. Use focused queries and ICP filters.",
      parameters: Type.Object({
        query: Type.String({ minLength: 1 }),
        industry: Type.Optional(Type.String({ minLength: 1 })),
        geography: Type.Optional(Type.String({ minLength: 1 })),
        employee_count: Type.Optional(Type.Array(Type.String({ minLength: 1 }), { maxItems: 20 })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 6 })),
      }),
    }),
    remoteTool({
      name: "get_company_profile",
      label: "Get company profile",
      description: "Get Deepline firmographic data for one company domain.",
      parameters: Type.Object({
        domain: Type.String({ minLength: 1 }),
      }),
    }),
    remoteTool({
      name: "get_company_events",
      label: "Get company events",
      description: "Find live company events such as jobs or financing for one domain.",
      parameters: Type.Object({
        domain: Type.String({ minLength: 1 }),
        categories: Type.Optional(Type.Array(Type.String({ minLength: 1 }), { maxItems: 20 })),
        query: Type.Optional(Type.String({ minLength: 1 })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 5 })),
      }),
    }),
    remoteTool({
      name: "search_web",
      label: "Search web",
      description: "Search the public web, recent news, or jobs through ScrapingDog.",
      parameters: Type.Object({
        query: Type.String({ minLength: 1 }),
        mode: Type.Optional(Type.Union([Type.Literal("search"), Type.Literal("news"), Type.Literal("jobs")])),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 5 })),
        recency_days: Type.Optional(Type.Integer({ minimum: 1, maximum: 3650 })),
      }),
    }),
    remoteTool({
      name: "fetch_page",
      label: "Fetch evidence page",
      description: "Fetch readable text from a public evidence URL to verify a fit or intent claim.",
      parameters: Type.Object({
        url: Type.String({ minLength: 8 }),
        max_chars: Type.Optional(Type.Integer({ minimum: 1000, maximum: 4000 })),
      }),
    }),
    defineTool({
      name: "submit_companies",
      label: "Submit companies",
      description: "Submit the final ranked companies exactly once. This is the terminal sourcing action.",
      parameters: Type.Object({
        companies: Type.Array(
          Type.Object({
            company_name: Type.String({ minLength: 1 }),
            company_website: Type.String({ minLength: 8 }),
            company_linkedin: Type.String(),
            industry: Type.String(),
            employee_count: Type.String(),
            country: Type.String(),
            state: Type.String(),
            fit_summary: Type.String({ minLength: 1 }),
            fit_evidence_urls: Type.Array(Type.String({ minLength: 8 })),
            intent_signals: Type.Array(
              Type.Object({
                matched_icp_signal: Type.Integer({ minimum: 0 }),
                description: Type.String({ minLength: 1 }),
                date: Type.String({ format: "date", pattern: "^\\d{4}-\\d{2}-\\d{2}$" }),
                why_now: Type.String({ minLength: 1 }),
                url: Type.String({ minLength: 8 }),
                snippet: Type.String({ minLength: 1 }),
              }),
              { minItems: 1 },
            ),
          }),
          { maxItems: maxCompanies },
        ),
      }),
      executionMode: "sequential",
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        if (submittedCompanies !== undefined) {
          return errorResult(new Error("submit_companies can be called only once"));
        }
        const validationError = validateCompanies(params.companies, maxCompanies);
        if (validationError) return errorResult(new Error(validationError));
        let remoteResult;
        try {
          remoteResult = await invokeRemote("submit_companies", { companies: params.companies }, signal);
        } catch (error) {
          return errorResult(error);
        }
        const accepted = Array.isArray(remoteResult?.companies) ? remoteResult.companies : params.companies;
        const remoteValidationError = validateCompanies(accepted, maxCompanies);
        if (remoteValidationError) return errorResult(new Error(remoteValidationError));
        submittedCompanies = structuredClone(accepted);
        queueMicrotask(() => ctx.abort());
        return {
          content: [{ type: "text", text: "Submission accepted. The run is complete." }],
          details: { companyCount: submittedCompanies.length },
        };
      },
    }),
  ];

  const credentials = new InMemoryCredentialStore();
  const modelRuntime = await ModelRuntime.create({
    credentials,
    modelsPath: null,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  await modelRuntime.setRuntimeApiKey("openrouter", openRouterKey);

  let model = modelRuntime.getModel("openrouter", modelId);
  if (!model) {
    const resolved = resolveCliModel({
      cliProvider: "openrouter",
      cliModel: modelId,
      cliThinking: "medium",
      modelRuntime,
    });
    if (resolved.error || !resolved.model) {
      throw new Error(resolved.error || `OpenRouter model ${modelId} is unavailable`);
    }
    model = resolved.model;
  }

  const cwd = process.cwd();
  const agentDir = process.env.BAKEOFF_PI_AGENT_DIR?.trim() || cwd;
  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: false },
    retry: { enabled: false },
  });
  const loader = new DefaultResourceLoader({
    cwd,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPromptOverride: () => systemPrompt,
    appendSystemPromptOverride: () => [],
  });
  await loader.reload();

  const toolNames = tools.map((tool) => tool.name);
  const { session } = await createAgentSession({
    cwd,
    agentDir,
    model,
    modelRuntime,
    thinkingLevel: "medium",
    resourceLoader: loader,
    sessionManager: SessionManager.inMemory(),
    settingsManager,
    noTools: "builtin",
    tools: toolNames,
    customTools: tools,
  });

  const usageLimitError = (stats) => {
    const aggregateInputTokens =
      stats.tokens.input + stats.tokens.cacheRead + stats.tokens.cacheWrite;
    if (stats.assistantMessages > maxTurns) {
      return Object.assign(new Error(`Pi exceeded the ${maxTurns}-turn limit`), {
        limitKind: "turn",
      });
    }
    if (aggregateInputTokens > maxInputTokens) {
      return Object.assign(
        new Error(
          `Pi exceeded the ${maxInputTokens}-aggregate-input-token limit (${aggregateInputTokens})`,
        ),
        { limitKind: "token" },
      );
    }
    if (stats.tokens.output > maxOutputTokens) {
      return Object.assign(new Error(`Pi exceeded the ${maxOutputTokens}-output-token limit`), {
        limitKind: "token",
      });
    }
    return undefined;
  };

  const writeResult = (companies, stats, enforcedLimit, executionError) => {
    const aggregateInputTokens =
      stats.tokens.input + stats.tokens.cacheRead + stats.tokens.cacheWrite;
    process.stdout.write(`${JSON.stringify({
      ok: !executionError,
      companies,
      error: executionError
        ? (executionError instanceof Error ? executionError.message : String(executionError))
        : undefined,
      usage: {
        input_tokens: stats.tokens.input,
        aggregate_input_tokens: aggregateInputTokens,
        output_tokens: stats.tokens.output,
        cache_read_tokens: stats.tokens.cacheRead,
        cache_write_tokens: stats.tokens.cacheWrite,
        total_tokens: stats.tokens.total,
        cost_usd: stats.cost,
        tool_calls: stats.toolCalls,
        provider_calls: providerCallCount,
        enforced_limit_error: enforcedLimit?.message,
      },
    })}\n`);
  };

  let limitError;
  const unsubscribe = session.subscribe((event) => {
    if (event.type !== "message_end") return;
    const stats = session.getSessionStats();
    limitError ||= usageLimitError(stats);
    if (limitError) void session.abort();
  });

  try {
    const activeTools = session.getActiveToolNames();
    const unexpectedTools = activeTools.filter((name) => !toolNames.includes(name));
    const missingTools = toolNames.filter((name) => !activeTools.includes(name));
    if (unexpectedTools.length || missingTools.length) {
      throw new Error(
        `Pi tool isolation failed (unexpected=${unexpectedTools.join(",")}; missing=${missingTools.join(",")})`,
      );
    }

    let timeout;
    let executionError;
    const deadline = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        void session.abort();
        reject(new Error(`Pi run exceeded ${timeoutMs / 1000} seconds`));
      }, timeoutMs);
    });
    try {
      await Promise.race([
        session.prompt(userPrompt, { expandPromptTemplates: false, source: "rpc" }),
        deadline,
      ]);
    } catch (error) {
      if (limitError?.limitKind === "turn" || (submittedCompanies === undefined && !limitError)) {
        executionError = limitError || error;
      }
    } finally {
      clearTimeout(timeout);
    }

    const stats = session.getSessionStats();
    limitError ||= usageLimitError(stats);
    executionError ||= limitError;
    if (submittedCompanies === undefined && !limitError) {
      executionError ||= new Error("Pi stopped without calling submit_companies");
    }
    if (executionError) {
      writeResult(null, stats, limitError, executionError);
      process.exitCode = 1;
      return;
    }
    writeResult(submittedCompanies || [], stats, limitError, undefined);
  } finally {
    unsubscribe();
    session.dispose();
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`Pi bakeoff worker failed: ${message}\n`);
  process.exitCode = 1;
});
