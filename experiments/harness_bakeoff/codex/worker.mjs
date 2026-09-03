import { Codex } from "@openai/codex-sdk";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_MODEL = "openai/gpt-5.6-sol";
const TOOL_NAMES = [
  "search_companies",
  "get_company_profile",
  "get_company_events",
  "search_web",
  "fetch_page",
  "submit_companies",
];
const MCP_SERVER = fileURLToPath(new URL("./sourcing_mcp.mjs", import.meta.url));

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

async function readInput() {
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    total += chunk.length;
    if (total > 1_000_000) throw new Error("ICP request exceeds 1 MB");
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) throw new Error("Expected one adapter request on stdin");
  const request = JSON.parse(raw);
  if (!request || Array.isArray(request) || typeof request !== "object") {
    throw new Error("Adapter request must be a JSON object");
  }
  return request;
}

function objectArguments(value) {
  if (value && !Array.isArray(value) && typeof value === "object") return value;
  if (typeof value === "string") {
    const parsed = JSON.parse(value);
    if (parsed && !Array.isArray(parsed) && typeof parsed === "object") return parsed;
  }
  throw new Error("submit_companies returned invalid arguments");
}

function usageReport(turn) {
  const usage = turn.usage;
  if (!usage) return {};
  const mcpCalls = turn.items.filter((item) => item.type === "mcp_tool_call");
  return {
    input_tokens: usage.input_tokens,
    cached_input_tokens: usage.cached_input_tokens,
    cache_write_input_tokens: usage.cache_write_input_tokens,
    output_tokens: usage.output_tokens,
    reasoning_output_tokens: usage.reasoning_output_tokens,
    total_tokens: (usage.input_tokens || 0) + (usage.output_tokens || 0),
    provider_calls: mcpCalls.filter((item) => item.tool !== "submit_companies").length,
    tool_calls: mcpCalls.length,
  };
}

async function main() {
  const request = await readInput();
  if (request.protocol !== "leadpoet.harness_bakeoff.codex.v1") {
    throw new Error("Unsupported Codex adapter protocol");
  }
  if (!request.icp || Array.isArray(request.icp) || typeof request.icp !== "object") {
    throw new Error("ICP must be a JSON object");
  }
  const prompt = typeof request.prompt === "string" ? request.prompt.trim() : "";
  const systemPrompt = typeof request.system_prompt === "string" ? request.system_prompt.trim() : "";
  if (!prompt || !systemPrompt) throw new Error("The shared system and user prompts are required");

  const openRouterKey = requiredEnvironment("OPENROUTER_API_KEY");
  requiredEnvironment("BAKEOFF_TOOL_URL");
  requiredEnvironment("BAKEOFF_TOOL_TOKEN");
  const model = process.env.BAKEOFF_OPENROUTER_MODEL?.trim() || DEFAULT_MODEL;
  const maxCompanies = positiveInteger("BAKEOFF_MAX_COMPANIES", 5, 5);
  const timeoutSeconds = positiveInteger("BAKEOFF_RUN_TIMEOUT_SECONDS", 720, 3600);
  const maxInputTokens = positiveInteger("BAKEOFF_MAX_INPUT_TOKENS", 120_000, 2_000_000);
  const maxOutputTokens = positiveInteger("BAKEOFF_MAX_OUTPUT_TOKENS", 15_000, 200_000);

  const workspace = await mkdtemp(path.join(os.tmpdir(), "leadpoet-codex-workspace-"));
  const codexHome = await mkdtemp(path.join(os.tmpdir(), "leadpoet-codex-home-"));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  try {
    const childEnvironment = {
      BAKEOFF_MAX_PROVIDER_CALLS: process.env.BAKEOFF_MAX_PROVIDER_CALLS || "30",
      BAKEOFF_TOOL_TIMEOUT_SECONDS: process.env.BAKEOFF_TOOL_TIMEOUT_SECONDS || "90",
      BAKEOFF_TOOL_TOKEN: process.env.BAKEOFF_TOOL_TOKEN,
      BAKEOFF_TOOL_URL: process.env.BAKEOFF_TOOL_URL,
      CODEX_HOME: codexHome,
      HOME: codexHome,
      LANG: process.env.LANG || "C.UTF-8",
      OPENROUTER_API_KEY: openRouterKey,
      PATH: process.env.PATH || "/usr/bin:/bin",
      TMPDIR: os.tmpdir(),
    };
    const codex = new Codex({
      env: childEnvironment,
      config: {
        model_provider: "openrouter",
        model_providers: {
          openrouter: {
            name: "OpenRouter",
            base_url: "https://openrouter.ai/api/v1",
            env_key: "OPENROUTER_API_KEY",
            wire_api: "responses",
            request_max_retries: 0,
            stream_max_retries: 0,
            supports_websockets: false,
          },
        },
        instructions: systemPrompt,
        history: { persistence: "none" },
        project_doc_max_bytes: 0,
        suppress_unstable_features_warning: true,
        include_apps_instructions: false,
        include_collaboration_mode_instructions: false,
        include_environment_context: false,
        include_permissions_instructions: false,
        features: {
          apps: false,
          auth_elicitation: false,
          browser_use: false,
          browser_use_external: false,
          browser_use_full_cdp_access: false,
          code_mode: {
            enabled: true,
            excluded_tool_namespaces: ["functions"],
          },
          code_mode_host: {
            enabled: true,
            disable_in_process_fallback: true,
          },
          code_mode_only: true,
          computer_use: false,
          goals: false,
          guardian_approval: false,
          hooks: false,
          image_generation: false,
          in_app_browser: false,
          in_app_local_automation: false,
          memories: false,
          mentions_v2: false,
          multi_agent: false,
          multi_agent_v2: false,
          plugin_sharing: false,
          plugins: false,
          recommended_plugins: false,
          remote_compaction_v2: false,
          remote_plugin: false,
          request_permissions_tool: false,
          shell_tool: false,
          shell_snapshot: false,
          skill_mcp_dependency_install: false,
          skill_search: false,
          sleep_tool: false,
          standalone_web_search: false,
          tool_call_mcp_elicitation: false,
          tool_suggest: false,
          unbounded_connection_retries: false,
          view_image: false,
          workspace_dependencies: false,
        },
        mcp_servers: {
          sourcing: {
            command: process.execPath,
            args: [MCP_SERVER],
            env_vars: [
              "BAKEOFF_TOOL_URL",
              "BAKEOFF_TOOL_TOKEN",
              "BAKEOFF_MAX_PROVIDER_CALLS",
              "BAKEOFF_TOOL_TIMEOUT_SECONDS"
            ],
            enabled: true,
            required: true,
            default_tools_approval_mode: "approve",
            enabled_tools: TOOL_NAMES,
            startup_timeout_sec: 10,
            tool_timeout_sec: positiveInteger("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90, 600),
          },
        },
      },
    });
    const thread = codex.startThread({
      model,
      modelReasoningEffort: "medium",
      sandboxMode: "read-only",
      workingDirectory: workspace,
      skipGitRepoCheck: true,
      networkAccessEnabled: false,
      webSearchMode: "disabled",
      approvalPolicy: "never",
    });
    let turn;
    try {
      turn = await thread.run(prompt, {
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(`Codex SDK run exceeded ${timeoutSeconds} seconds`, { cause: error });
      }
      throw error;
    }

    const usage = turn.usage;
    const reportedUsage = usageReport(turn);
    try {
      const forbiddenItems = turn.items.filter((item) =>
        ["command_execution", "file_change", "web_search"].includes(item.type),
      );
      if (forbiddenItems.length) {
        throw new Error(`Codex tool isolation failed: observed ${forbiddenItems[0].type}`);
      }
      const foreignMcp = turn.items.find(
        (item) => item.type === "mcp_tool_call" && (item.server !== "sourcing" || !TOOL_NAMES.includes(item.tool)),
      );
      if (foreignMcp) throw new Error("Codex invoked an unapproved MCP tool");
      const submissions = turn.items.filter(
        (item) => item.type === "mcp_tool_call" && item.server === "sourcing" && item.tool === "submit_companies" && item.status === "completed",
      );
      if (submissions.length !== 1) {
        const observed = turn.items;
        throw new Error(
          `Codex must complete submit_companies exactly once; observed ${submissions.length}; ` +
          `items=${JSON.stringify(observed).slice(0, 2000)}; ` +
          `final=${String(turn.finalResponse || "").slice(0, 1000)}`
        );
      }
      const submissionIndex = turn.items.indexOf(submissions[0]);
      const postSubmissionTool = turn.items
        .slice(submissionIndex + 1)
        .find((item) => item.type === "mcp_tool_call");
      if (postSubmissionTool) {
        throw new Error("Codex attempted another tool call after terminal submit_companies");
      }

      const limitErrors = [];
      if (usage && usage.input_tokens > maxInputTokens) {
        limitErrors.push(
          `input token limit exceeded: ${usage.input_tokens} > ${maxInputTokens}`,
        );
      }
      if (usage && usage.output_tokens > maxOutputTokens) {
        limitErrors.push(
          `output token limit exceeded: ${usage.output_tokens} > ${maxOutputTokens}`,
        );
      }
      const submitted = objectArguments(submissions[0].arguments);
      if (!Array.isArray(submitted.companies) || submitted.companies.length > maxCompanies) {
        throw new Error("submit_companies returned an invalid company list");
      }
      process.stdout.write(`${JSON.stringify({
        ok: true,
        companies: submitted.companies,
        usage: { ...reportedUsage, limit_errors: limitErrors },
      })}\n`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stdout.write(`${JSON.stringify({
        ok: false,
        error: message,
        companies: null,
        usage: reportedUsage,
      })}\n`);
      process.exitCode = 1;
    }
  } finally {
    clearTimeout(timer);
    await Promise.all([
      rm(workspace, { recursive: true, force: true }),
      rm(codexHome, { recursive: true, force: true }),
    ]);
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`Codex bakeoff worker failed: ${message}\n`);
  process.exitCode = 1;
});
