"use client";

import type { Run } from "@langchain/langgraph-sdk";
import { useEffect, useRef, useState } from "react";

import { getAPIClient, rememberReconnectRun } from "../api/api-client";

/**
 * The SDK's `useStream` reconnect-on-mount only reattaches a run the *current
 * tab* started: it persists the run id under a sessionStorage key at submit time
 * and rejoins it on reload. A new tab, a closed browser, or a shared link has no
 * such key, so reopening a thread whose run is still active server-side (the
 * WebUI submits with `onDisconnect: "continue"`) renders from stale history:
 * `isLoading` stays false, the last turn looks settled ("Completed in …"), and
 * dangling `task` tool calls surface as "Subtask failed" even though the
 * subagents are still running.
 *
 * This rediscovers the active run on mount and rejoins its SSE stream — the
 * same `joinStream` path the SDK uses for same-tab reconnects — so a fresh
 * viewer picks up the live stream, the gap-recovery machinery reloads durable
 * state, and the turn stays loading until the run truly ends.
 */

const ACTIVE_RUN_STATUSES = new Set<Run["status"]>(["pending", "running"]);

/**
 * Pick the newest active run from a thread's run list (the backend returns
 * newest-first). At most one run per thread can be active — the runs table
 * enforces a unique active row — so the first pending/running entry is it.
 * Returns `undefined` when the thread is idle or no active run is in the page.
 */
export function pickActiveRun(runs: Run[] | undefined): Run | undefined {
  if (!runs || runs.length === 0) {
    return undefined;
  }
  return runs.find((run) => ACTIVE_RUN_STATUSES.has(run.status));
}

/** Minimal shape of the SDK `useStream` joinStream return. */
export type JoinActiveRun = (
  runId: string,
  lastEventId?: string,
) => Promise<void> | void;

/**
 * On mount (or thread switch), if no stream is already live and the SDK's
 * same-tab reconnect key is absent, look up the thread's runs and rejoin the
 * active one. Best-effort: every race is owned by existing machinery — a run
 * that finished before the join is short-circuited by the api-client's terminal
 * preflight; an evicted buffer triggers `stream_replay_gap` recovery; a
 * non-owning worker 409 is treated as inactive. Joining is disconnect-safe
 * because the SDK sends `cancel_on_disconnect=0`, so a viewer leaving never
 * aborts work it did not start.
 */
export function useActiveRunRejoin({
  threadId,
  joinStream,
  isLoading,
  isMock,
}: {
  threadId: string | null | undefined;
  joinStream: JoinActiveRun | undefined;
  isLoading: boolean;
  isMock?: boolean;
}): string | null {
  // The SDK recreates `joinStream` every render; read it through a ref so the
  // effect does not re-run on identity churn, only on the real signals.
  const joinStreamRef = useRef(joinStream);
  joinStreamRef.current = joinStream;
  // One discovery attempt per thread: the page-load reattach is a one-shot. A
  // run that starts *after* load is owned by the submitting tab's own stream.
  const attemptedRef = useRef<string | null>(null);
  // The active run id this viewer rejoined, exposed so a dangling `task` tool
  // call whose owning run is still live renders `in_progress` (not `failed`)
  // during the brief window before the joined stream flips `isLoading`, and on
  // join failure. Cleared on thread change; once a run ends its `task` calls
  // gain terminal ToolMessages, so a stale id is harmless.
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  useEffect(() => {
    setActiveRunId(null);
  }, [threadId]);

  useEffect(() => {
    if (!threadId || isMock || isLoading) {
      return;
    }
    if (typeof window === "undefined") {
      return;
    }
    // The SDK's own reconnectOnMount owns the same-tab, key-present case.
    if (window.sessionStorage.getItem(`lg:stream:${threadId}`)) {
      return;
    }
    if (attemptedRef.current === threadId) {
      return;
    }
    attemptedRef.current = threadId;

    const client = getAPIClient(isMock);
    let cancelled = false;
    void (async () => {
      try {
        const runs = await client.runs.list(threadId);
        if (cancelled) {
          return;
        }
        const active = pickActiveRun(runs);
        if (!active) {
          return;
        }
        // Re-check after the await: a submit may have started streaming, or the
        // SDK may have written its own same-tab reconnect key meanwhile.
        if (
          !joinStreamRef.current ||
          window.sessionStorage.getItem(`lg:stream:${threadId}`)
        ) {
          return;
        }
        // Surface the active run before joining so the subtask fallback applies
        // even if the join has not yet flipped `isLoading` (or fails to).
        setActiveRunId(active.run_id);
        // Write the SDK reconnect key so Stop / cancel / cleanup semantics
        // apply to this rejoined run exactly like a submitting tab (the SDK's
        // stop() reads this key to find the run to cancel).
        rememberReconnectRun(threadId, active.run_id);
        await joinStreamRef.current(active.run_id);
      } catch {
        // Discovery is best-effort: the run still finishes server-side and a
        // later refresh surfaces the final state.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [threadId, isLoading, isMock]);

  return activeRunId;
}
