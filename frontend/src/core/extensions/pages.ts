import type { PluginSurface } from "./contracts";
import { activeFrontendExtensions, type LoadedContribution } from "./registry";

/** Plugins register identifiers, never arbitrary host paths or external links. */
export function pluginPagePath(namespace: string, surfaceId: string) {
  return `/workspace/extensions/${encodeURIComponent(namespace)}/${encodeURIComponent(surfaceId)}`;
}

export function pluginPageTitle(surface: PluginSurface, locale: string) {
  return (
    (locale.startsWith("zh") ? surface.navigation?.labelZh : undefined) ??
    surface.navigation?.label ??
    surface.title
  );
}

/** Tab value of a plugin's project tab, namespaced so it can never shadow a host tab. */
export function pluginProjectTabValue(namespace: string, surfaceId: string) {
  return `plugin:${namespace}:${surfaceId}`;
}

// [argus patch #112] Project tabs and Agents page tabs share one helper and
// the same namespaced values.
function pluginTabs(
  entries: LoadedContribution[],
  slot: "project-tab" | "agents-tab",
) {
  return activeFrontendExtensions(entries).flatMap(
    ({ contribution, extension }) =>
      (extension.surfaces ?? [])
        .filter((surface) => surface.slot === slot)
        .map((surface) => ({
          contribution,
          surface,
          value: pluginProjectTabValue(contribution.namespace, surface.id),
        })),
  );
}

export function pluginProjectTabs(entries: LoadedContribution[]) {
  return pluginTabs(entries, "project-tab");
}

/** [argus patch #112] Enabled, loaded plugins' `agents-tab` surfaces, in plugin order. */
export function pluginAgentsTabs(entries: LoadedContribution[]) {
  return pluginTabs(entries, "agents-tab");
}

export function pluginPages(entries: LoadedContribution[]) {
  return activeFrontendExtensions(entries).flatMap(
    ({ contribution, extension }) =>
      (extension.surfaces ?? [])
        .filter((surface) => surface.slot === "page")
        .map((surface) => ({
          contribution,
          surface,
          href: pluginPagePath(contribution.namespace, surface.id),
          icon: surface.navigation?.icon ?? extension.icon,
        })),
  );
}
