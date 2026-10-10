"use client";

import { useFrontendExtensions } from "@/core/extensions/hooks";
import { pluginProjectTabs } from "@/core/extensions/pages";

/** Enabled, loaded plugins' `project-tab` surfaces, in plugin order. */
export function useProjectPluginTabs() {
  const query = useFrontendExtensions();
  return pluginProjectTabs(query.data ?? []);
}
