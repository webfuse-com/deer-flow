"use client";

import { useFrontendExtensions } from "@/core/extensions/hooks";
import { pluginAgentsTabs } from "@/core/extensions/pages";

/** [argus patch #112] Enabled, loaded plugins' `agents-tab` surfaces, in plugin order. */
export function useAgentsPluginTabs() {
  const query = useFrontendExtensions();
  return pluginAgentsTabs(query.data ?? []);
}
