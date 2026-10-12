"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef } from "react";
import { toast } from "sonner";

import { useI18n } from "@/core/i18n/hooks";
import { createThread } from "@/core/threads/api";
import { INFINITE_THREADS_QUERY_KEY_PREFIX } from "@/core/threads/hooks";

/**
 * Project-scoped new chat: `…/chats/new?project={id}` assigns the thread to
 * the project on the FIRST submit. Shared by the plain chat page and the
 * agent chat page ([argus patch #122]: Argus opens every new chat on the
 * citizen's Atlas agent page, which previously ignored `?project=`).
 *
 * Only meaningful while the thread is still lazy (`isNewThread`); once
 * materialized the URL is replaced with the thread route and the param is
 * gone. Invalid ids are dropped by backend admission.
 */
export function useProjectNewChat({
  threadId,
  isNewThread,
}: {
  threadId: string;
  isNewThread: boolean;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const projectParam = isNewThread ? searchParams.get("project") : null;

  // Born assigned: pre-create the thread row with its project so the sidebar
  // lists it under the project immediately. Idempotent server-side on
  // `thread_id`, so retrying the same first message (same `threadId` while
  // `isNewThread`) reuses the existing row instead of double-creating. This
  // is the sole membership channel — run requests never carry the project
  // key.
  //
  // Also runs before InputBox issues a `/goal <condition>` PUT (via
  // `onPrepareThread`): the goal endpoint materializes a missing thread row
  // itself, and an unassigned row would make this later idempotent create a
  // membership no-op.
  const ensureProjectThread = useCallback(async () => {
    if (!projectParam) {
      return;
    }
    try {
      const created = await createThread(threadId, projectParam);
      // Keep confirmed membership available while the first metadata read is pending
      // or fails after the composer materializes this new project thread.
      queryClient.setQueryData(
        ["thread", "metadata", threadId, false],
        created,
      );
      void queryClient.invalidateQueries({
        queryKey: INFINITE_THREADS_QUERY_KEY_PREFIX,
      });
    } catch (error) {
      // Any failure (e.g. the project was deleted/archived between page load
      // and submit): do NOT submit unassigned. Reject so PromptInput keeps
      // the composer's text for a retry; the send in-flight guard is never
      // engaged on this path, so retrying works immediately.
      toast.error(t.projects.projectUnavailable);
      throw error;
    }
  }, [threadId, projectParam, queryClient, t]);

  // Submission fence. The cleanup runs when `threadId` changes (conversation
  // switch on a persisted page — sidebar navigation keeps the page mounted),
  // when the new-chat project scope changes (the sidebar "New chat" link can
  // drop `?project=` without a pathname change), or on unmount. A submit
  // awaits the project pre-create before sending, and a navigation during
  // that await must not let the stale continuation start a run for the
  // abandoned conversation: its onStart would rewrite the newly selected
  // conversation's URL and its completion would clear the new composer.
  const submissionEpochRef = useRef(0);
  useEffect(() => {
    return () => {
      submissionEpochRef.current += 1;
    };
  }, [threadId, projectParam]);

  /**
   * Await the project pre-create, then reject (silently — the user has
   * already moved on, so a toast would land on the new conversation) when
   * the conversation changed meanwhile. PromptInput keeps the composer text.
   */
  const prepareSubmission = useCallback(async () => {
    const submissionEpoch = submissionEpochRef.current;
    await ensureProjectThread();
    if (submissionEpochRef.current !== submissionEpoch) {
      throw new Error("thread-submission-stale");
    }
  }, [ensureProjectThread]);

  return { projectParam, ensureProjectThread, prepareSubmission };
}
