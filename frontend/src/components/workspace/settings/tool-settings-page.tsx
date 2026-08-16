"use client";

import { Badge } from "@/components/ui/badge";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemTitle,
} from "@/components/ui/item";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/core/i18n/hooks";
import { MCPConfigRequestError } from "@/core/mcp/api";
import { useMCPConfig, useEnableMCPServer, useSystemTools } from "@/core/mcp/hooks";
import type { MCPServerConfig, SystemTool } from "@/core/mcp/types";
import { env } from "@/env";

import { SettingsSection } from "./settings-section";

export function ToolSettingsPage() {
  const { t } = useI18n();
  const { tools: systemTools, isLoading: isSystemLoading } = useSystemTools();
  const { config, isLoading, error } = useMCPConfig();
  const adminRequired =
    error instanceof MCPConfigRequestError && error.isAdminRequired;

  return (
    <SettingsSection
      title={t.settings.tools.title}
      description={t.settings.tools.description}
    >
      <div className="flex flex-col gap-6">
        {/* System & Built-in Tools */}
        <div className="flex flex-col gap-3">
          <div>
            <h4 className="text-sm font-semibold">{t.settings.tools.systemToolsTitle}</h4>
            <p className="text-muted-foreground text-xs">{t.settings.tools.systemToolsDescription}</p>
          </div>
          {isSystemLoading ? (
            <div className="text-muted-foreground text-sm">{t.common.loading}</div>
          ) : (
            <SystemToolsList tools={systemTools} />
          )}
        </div>

        {/* MCP Tool Servers */}
        <div className="flex flex-col gap-3">
          <div>
            <h4 className="text-sm font-semibold">{t.settings.tools.mcpServersTitle}</h4>
            <p className="text-muted-foreground text-xs">{t.settings.tools.mcpServersDescription}</p>
          </div>
          {isLoading ? (
            <div className="text-muted-foreground text-sm">{t.common.loading}</div>
          ) : adminRequired ? (
            <div className="text-muted-foreground text-sm">
              {t.settings.tools.adminRequired}
            </div>
          ) : error ? (
            <div>Error: {error.message}</div>
          ) : (
            config && <MCPServerList servers={config.mcp_servers} />
          )}
        </div>
      </div>
    </SettingsSection>
  );
}

function SystemToolsList({ tools }: { tools: SystemTool[] }) {
  if (!tools || tools.length === 0) {
    return null;
  }

  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {tools.map((tool) => (
        <Item className="w-full" variant="outline" key={tool.name}>
          <ItemContent>
            <ItemTitle>
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs">{tool.name}</span>
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0 font-normal">
                  {tool.group}
                </Badge>
              </div>
            </ItemTitle>
            {tool.description && (
              <ItemDescription className="line-clamp-2 text-xs">
                {tool.description}
              </ItemDescription>
            )}
          </ItemContent>
        </Item>
      ))}
    </div>
  );
}

function MCPServerList({
  servers,
}: {
  servers?: Record<string, MCPServerConfig>;
}) {
  const { t } = useI18n();
  const { isPending, mutate: enableMCPServer } = useEnableMCPServer();
  const entries = Object.entries(servers ?? {});
  if (entries.length === 0) {
    return (
      <div className="text-muted-foreground text-sm">
        {t.settings.tools.empty}
      </div>
    );
  }
  return (
    <div className="flex w-full flex-col gap-4">
      {entries.map(([name, config]) => (
        <Item className="w-full" variant="outline" key={name}>
          <ItemContent>
            <ItemTitle>
              <div className="flex items-center gap-2">
                <div>{name}</div>
              </div>
            </ItemTitle>
            <ItemDescription className="line-clamp-4">
              {config.description}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Switch
              checked={config.enabled}
              disabled={
                env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" || isPending
              }
              onCheckedChange={(checked) =>
                enableMCPServer({ serverName: name, enabled: checked })
              }
            />
          </ItemActions>
        </Item>
      ))}
    </div>
  );
}
