import type { AgentSource } from "@/core/agents/types";

export type SubagentSource = "builtin" | "config" | "managed";

/**
 * [argus patch #111] Where a managed definition is kept, when its store
 * reports it (e.g. the GitHub copy); the agents' source shape from #110.
 */
export type SubagentStoredIn = AgentSource;

export interface Subagent {
  name: string;
  display_name: string | null;
  description: string;
  system_prompt: string | null;
  tools: string[] | null;
  disallowed_tools: string[] | null;
  skills: string[] | null;
  model: string;
  max_turns: number;
  timeout_seconds: number;
  enabled: boolean;
  /** [argus patch #111] Extended thinking; null = the default (off). */
  thinking_enabled?: boolean | null;
  source: SubagentSource;
  editable: boolean;
  conflict: boolean;
  config_overrides: Record<string, unknown>;
  /** [argus patch #111] Omitted when the store reports no location. */
  stored_in?: SubagentStoredIn | null;
}

export interface CreateManagedSubagentRequest {
  name: string;
  display_name?: string | null;
  description: string;
  system_prompt: string;
  tools?: string[] | null;
  disallowed_tools?: string[] | null;
  skills?: string[] | null;
  model?: string;
  max_turns?: number;
  timeout_seconds?: number;
  enabled?: boolean;
  thinking_enabled?: boolean | null; // [argus patch #111]
}

export type UpdateManagedSubagentRequest = Partial<
  Omit<CreateManagedSubagentRequest, "name">
>;
