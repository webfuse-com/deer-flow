import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import type { ThreadTokenUsageResponse } from "./types";

export async function fetchThreadTokenUsage(
  threadId: string,
): Promise<ThreadTokenUsageResponse | null> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/${encodeURIComponent(threadId)}/token-usage`,
    {
      method: "GET",
    },
  );

  if (!response.ok) {
    if (response.status === 403 || response.status === 404) {
      return null;
    }
    throw new Error("Failed to load thread token usage.");
  }

  return (await response.json()) as ThreadTokenUsageResponse;
}

/**
 * Acquire (or re-acquire) this thread's debug sandbox and return the relative
 * URL of its AIO sandbox UI. The URL is served by the per-project nginx
 * `/debug-sandbox/<hash>/` location behind the same auth boundary as the app.
 *
 * Returns `null` when the deployment has no container-backed sandbox provider
 * (409) so callers can hide the Debug affordance instead of surfacing an error.
 */
export async function acquireDebugSandbox(
  threadId: string,
): Promise<{ hash: string; url: string } | null> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/${encodeURIComponent(threadId)}/debug-sandbox`,
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    // 409: provider is not container-backed — feature unavailable, not an error.
    if (response.status === 409) {
      return null;
    }
    throw new Error("Failed to open debug sandbox for this thread.");
  }

  return (await response.json()) as { hash: string; url: string };
}
