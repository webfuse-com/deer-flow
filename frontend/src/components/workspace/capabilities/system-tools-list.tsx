"use client";

// [argus] Read-only list of the configured system and built-in tools. It lived
// on Settings > Tools until upstream moved tool management into the Capability
// Center (#5468); it now renders under the plugin gallery in its own file so
// future syncs do not conflict on upstream's plugin manager.

import { Badge } from "@/components/ui/badge";
import {
  Item,
  ItemContent,
  ItemDescription,
  ItemTitle,
} from "@/components/ui/item";
import { useI18n } from "@/core/i18n/hooks";
import { useSystemTools } from "@/core/mcp/hooks";

export function SystemToolsSection() {
  const { t } = useI18n();
  const { tools, isLoading } = useSystemTools();

  if (!isLoading && tools.length === 0) {
    return null;
  }

  return (
    <section className="flex flex-col gap-3">
      <div>
        <h4 className="text-sm font-semibold">
          {t.settings.tools.systemToolsTitle}
        </h4>
        <p className="text-muted-foreground text-xs">
          {t.settings.tools.systemToolsDescription}
        </p>
      </div>
      {isLoading ? (
        <div className="text-muted-foreground text-sm">{t.common.loading}</div>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {tools.map((tool) => (
            <Item className="w-full" variant="outline" key={tool.name}>
              <ItemContent>
                <ItemTitle>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs">{tool.name}</span>
                    <Badge
                      variant="secondary"
                      className="px-1.5 py-0 text-[10px] font-normal"
                    >
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
      )}
    </section>
  );
}
