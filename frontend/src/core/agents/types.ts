import type { KnowledgeScopeSnapshot } from "@/core/knowledge";

export interface AgentModelSettings {
  temperature?: number | null;
  max_tokens?: number | null;
}

export type ReasoningEffort = "low" | "medium" | "high";

/**
 * [argus patch #110] Where the agent definition is kept, when the backend's
 * agent store reports it (absent for the stock stores). ``commit`` is the
 * commit that holds the definition the response describes.
 */
export interface AgentSource {
  kind: "github";
  url: string;
  path?: string;
  commit?: string | null;
}

export interface Agent {
  name: string;
  display_name?: string | null;
  description: string;
  model: string | null;
  tool_groups: string[] | null;
  skills: string[] | null;
  mcp_plugins?: string[] | null;
  knowledge_scope?: KnowledgeScopeSnapshot | null;
  allowed_subagents?: string[] | null;
  model_settings?: AgentModelSettings | null;
  thinking_enabled?: boolean | null;
  reasoning_effort?: ReasoningEffort | null;
  soul?: string | null;
  /** [argus patch #110] Omitted when the store reports no source. */
  source?: AgentSource | null;
}

export interface CreateAgentRequest {
  name: string;
  display_name?: string | null;
  description?: string;
  model?: string | null;
  tool_groups?: string[] | null;
  skills?: string[] | null;
  mcp_plugins?: string[] | null;
  knowledge_scope?: KnowledgeScopeSnapshot | null;
  allowed_subagents?: string[] | null;
  model_settings?: AgentModelSettings | null;
  thinking_enabled?: boolean | null;
  reasoning_effort?: ReasoningEffort | null;
  soul?: string;
}

export interface PortableAgentDefinition extends CreateAgentRequest {
  name: string;
  memory_enabled?: boolean;
}

export interface AgentPackage {
  format: "deerflow.custom-agent";
  version: 1;
  agent: PortableAgentDefinition;
}

export interface UpdateAgentRequest {
  display_name?: string | null;
  description?: string | null;
  model?: string | null;
  tool_groups?: string[] | null;
  skills?: string[] | null;
  mcp_plugins?: string[] | null;
  knowledge_scope?: KnowledgeScopeSnapshot | null;
  allowed_subagents?: string[] | null;
  model_settings?: AgentModelSettings | null;
  thinking_enabled?: boolean | null;
  reasoning_effort?: ReasoningEffort | null;
  soul?: string | null;
}
