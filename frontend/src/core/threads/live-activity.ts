"use client";

// [argus patch #103] Live run activity for open tabs.
//
// Not upstream's ./activity.ts (GET /api/thread-activity, 2026-10): that one
// polls the run-change clock every 15 s so the sidebar learns about
// SERVER-started runs and read state. It neither covers a run started in
// another tab nor streams a reply into an open thread. Events here carry
// origin_kind; a server-started run's sidebar refresh is left to upstream.
//
// A tab streams only the runs it starts (or one it finds active on load). A
// run started elsewhere (a build wake-up, Telegram, a schedule) never reached
// a tab that already had the thread open. The gateway's
// GET /api/threads/activity stream names each run admitted or finished on the
// viewer's threads; this hook refetches that thread's run list, so the
// existing rejoin (useThreadStream's activeRunId effect) streams the reply in
// live, and refreshes the history and the sidebar.
//
// Load: one stream per visible workspace tab (closed while the tab is hidden,
// resynced when it shows again), a few small refetches per run, and an
// exponential back-off when the gateway does not offer the stream.

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { getBackendBaseURL } from "@/core/config";

import {
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  threadHistoryQueryKey,
} from "./hooks";

export type LiveRunEvent = {
  thread_id: string;
  run_id: string;
  status: string;
  updated_at?: string | null;
  /** null for interactive runs; set for server-started ones (schedule, IM, ...). */
  origin_kind?: string | null;
};

// The gateway's terminal RunStatus values.
const TERMINAL = new Set(["success", "error", "interrupted", "timeout"]);
const SIDEBAR_DEBOUNCE_MS = 1500;
const RETRY_MIN_MS = 2000;
const RETRY_MAX_MS = 5 * 60 * 1000;

/** The query keys one run event makes stale. */
export function liveActivityInvalidations(
  event: LiveRunEvent,
): ReadonlyArray<readonly unknown[]> {
  const keys: Array<readonly unknown[]> = [["thread", event.thread_id]];
  if (TERMINAL.has(event.status)) {
    keys.push(threadHistoryQueryKey(event.thread_id));
  }
  return keys;
}

function refreshSidebar(queryClient: QueryClient) {
  void queryClient.invalidateQueries({
    queryKey: INFINITE_THREADS_QUERY_KEY_PREFIX,
  });
  void queryClient.invalidateQueries({ queryKey: ["threads", "search"] });
}

/** Everything a tab shows may have moved while it was not listening. */
function resync(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: ["thread"] });
  void queryClient.invalidateQueries({ queryKey: ["thread-messages"] });
  refreshSidebar(queryClient);
}

export function useLiveThreadActivity() {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      return;
    }
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let sidebarTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = RETRY_MIN_MS;
    let connectedOnce = false;
    let stopped = false;

    const scheduleSidebar = () => {
      if (sidebarTimer) return;
      sidebarTimer = setTimeout(() => {
        sidebarTimer = null;
        refreshSidebar(queryClient);
      }, SIDEBAR_DEBOUNCE_MS);
    };

    const close = () => {
      source?.close();
      source = null;
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const open = () => {
      if (stopped || source || document.visibilityState === "hidden") return;
      source = new EventSource(`${getBackendBaseURL()}/api/threads/activity`, {
        withCredentials: true,
      });
      source.addEventListener("ready", () => {
        retryMs = RETRY_MIN_MS;
        // A reconnect (or a tab shown again) may have missed events.
        if (connectedOnce) resync(queryClient);
        connectedOnce = true;
      });
      source.addEventListener("resync", () => resync(queryClient));
      source.addEventListener("run", (message) => {
        let event: LiveRunEvent;
        try {
          event = JSON.parse((message as MessageEvent<string>).data);
        } catch {
          return;
        }
        if (!event?.thread_id) return;
        for (const queryKey of liveActivityInvalidations(event)) {
          void queryClient.invalidateQueries({ queryKey });
        }
        // Upstream's sidebar feed already refreshes the list for server-started runs.
        if (!event.origin_kind) scheduleSidebar();
      });
      source.onerror = () => {
        // Our own back-off instead of EventSource's fixed 3 s retry, so a
        // gateway without the stream (or a restart) is not hammered.
        close();
        if (stopped) return;
        retryTimer = setTimeout(open, retryMs);
        retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
      };
    };

    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        close();
      } else {
        open();
      }
    };

    open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      close();
      if (sidebarTimer) clearTimeout(sidebarTimer);
    };
  }, [queryClient]);
}

/** Mounted once in the workspace: renders nothing. */
export function LiveThreadActivityBridge() {
  useLiveThreadActivity();
  return null;
}
