export interface MCPServerConfig extends Record<string, unknown> {
  enabled: boolean;
  description: string;
}

export interface MCPConfig {
  mcp_servers: Record<string, MCPServerConfig>;
}

export interface SystemTool {
  name: string;
  group: string;
  description: string;
}

export interface SystemToolsResponse {
  tools: SystemTool[];
}
