import type { ManagedModelProvider, ModelDraft } from "./management";

/**
 * [argus] Settings > Models hides upstream's provider presets (#6757). Every
 * Atlas owner is an admin of their gateway, and a preset turns one pasted API
 * key into a direct provider route outside LiteLLM and its spend tracking.
 * The custom OpenAI-compatible endpoint (pointed at LiteLLM) stays offered.
 * Set to true to show the presets again.
 */
export const MODEL_PROVIDER_PRESETS_VISIBLE = false;

/**
 * The providers the model editor offers. With the presets hidden only the
 * custom endpoint remains, plus `keep`: an edited model's own provider, so a
 * model saved on a preset stays editable.
 */
export function offeredModelProviders(
  providers: readonly ManagedModelProvider[],
  presetsVisible: boolean,
  keep?: string,
): ManagedModelProvider[] {
  return presetsVisible
    ? [...providers]
    : providers.filter((item) => item.custom_endpoint || item.id === keep);
}

/** A new model starts on the first offered provider (upstream: OpenAI). */
export function draftOnOfferedProvider(
  draft: ModelDraft,
  providers: readonly ManagedModelProvider[],
): ModelDraft {
  const first = providers[0];
  if (!first || providers.some((item) => item.id === draft.provider)) {
    return draft;
  }
  return {
    ...draft,
    provider: first.id,
    ...(first.custom_endpoint ? { base_url: "" } : {}),
  };
}
