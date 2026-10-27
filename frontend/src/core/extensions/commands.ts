import type { CommandContext, CommandEntry } from "./contracts";
import { activeFrontendExtensions, type LoadedContribution } from "./registry";
import {
  bindFrontendServices,
  conversationText,
  latestVisibleAnswer,
} from "./services";

// [argus patch #167] Command-palette groups contributed by browser extensions.
// Same fencing as mentions.ts: one deadline per provider, a failing provider
// drops only its own group, and every entry is re-validated here because the
// palette renders and navigates to it.

export const MAX_COMMAND_ITEMS = 300;
const PROVIDER_TIMEOUT_MS = 3000;

const bounded = (value: unknown, limit: number): value is string =>
  typeof value === "string" && !!value.trim() && value.length <= limit;

/** An https: URL (new tab) or a `/workspace/` path (in-app); anything else is refused. */
export function commandHref(
  href: unknown,
): { kind: "external" | "internal"; href: string } | null {
  if (!bounded(href, 2048)) return null;
  if (href.startsWith("/workspace/") && !/[\\\s]/.test(href))
    return { kind: "internal", href };
  try {
    const url = new URL(href);
    return url.protocol === "https:" && !url.username && !url.password
      ? { kind: "external", href: url.href }
      : null;
  } catch {
    return null;
  }
}

export type CommandGroupItem = Omit<CommandEntry, "href"> & {
  href: string;
  kind: "external" | "internal";
};
export type CommandGroup = {
  key: string;
  label: string;
  icon?: string;
  items: CommandGroupItem[];
};

function validEntries(candidates: unknown): CommandGroupItem[] {
  if (!Array.isArray(candidates)) throw new Error("Invalid command results");
  const seen = new Set<string>();
  const items: CommandGroupItem[] = [];
  for (const candidate of candidates.slice(0, MAX_COMMAND_ITEMS)) {
    const entry = candidate as Partial<CommandEntry> | null;
    if (!entry || !bounded(entry.id, 512) || !bounded(entry.label, 120))
      continue;
    if (seen.has(entry.id)) continue;
    const target = commandHref(entry.href);
    if (!target) continue;
    seen.add(entry.id);
    const keywords = Array.isArray(entry.keywords)
      ? entry.keywords.filter((k) => bounded(k, 120)).slice(0, 16)
      : [];
    items.push({
      id: entry.id,
      label: entry.label,
      ...(bounded(entry.description, 240)
        ? { description: entry.description }
        : {}),
      keywords,
      href: target.href,
      kind: target.kind,
    });
  }
  return items;
}

export async function loadExtensionCommands(
  entries: LoadedContribution[],
  context: Pick<CommandContext, "locale" | "signal">,
): Promise<{ groups: CommandGroup[]; failed: boolean }> {
  context.signal.throwIfAborted();
  const providers = activeFrontendExtensions(entries)
    .flatMap(({ contribution, extension }) =>
      (extension.commandProviders ?? []).map((provider) => ({
        contribution,
        provider,
      })),
    )
    .slice(0, 16);
  const results = await Promise.all(
    providers.map(async ({ contribution, provider }) => {
      const abort = new AbortController();
      const cancel = () => abort.abort();
      context.signal.addEventListener("abort", cancel, { once: true });
      let timeout: ReturnType<typeof setTimeout> | undefined;
      let onAbort: (() => void) | undefined;
      try {
        const deadline = new Promise<never>((_, reject) => {
          onAbort = () => reject(new Error("Command load cancelled"));
          abort.signal.addEventListener("abort", onAbort, { once: true });
          timeout = setTimeout(() => abort.abort(), PROVIDER_TIMEOUT_MS);
        });
        const services = bindFrontendServices(
          {
            conversationText,
            latestVisibleAnswer,
            showMessage: () => undefined,
          },
          contribution,
          abort.signal,
        );
        const candidates = await Promise.race([
          Promise.resolve().then(() => {
            abort.signal.throwIfAborted();
            return provider.items({
              locale: context.locale,
              namespace: contribution.namespace,
              settings: contribution.settings,
              signal: abort.signal,
              callBackend: async (action, payload) => {
                abort.signal.throwIfAborted();
                const result = await services.callBackend(action, payload);
                abort.signal.throwIfAborted();
                return result;
              },
            });
          }),
          deadline,
        ]);
        const group: CommandGroup = {
          key: `${contribution.namespace}/${provider.id}`,
          label: provider.label,
          ...(provider.icon ? { icon: provider.icon } : {}),
          items: validEntries(candidates),
        };
        return { group, failed: false };
      } catch {
        return { group: null, failed: true };
      } finally {
        clearTimeout(timeout);
        if (onAbort) abort.signal.removeEventListener("abort", onAbort);
        abort.abort();
        context.signal.removeEventListener("abort", cancel);
      }
    }),
  );
  context.signal.throwIfAborted();
  return {
    groups: results.flatMap((result) =>
      result.group?.items.length ? [result.group] : [],
    ),
    failed: results.some((result) => result.failed),
  };
}
