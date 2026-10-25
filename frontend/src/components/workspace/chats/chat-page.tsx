"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { ArtifactTrigger } from "@/components/workspace/artifacts";
import { BrowserTrigger } from "@/components/workspace/browser-view";
import { ContextUsageBadge } from "@/components/workspace/context-usage-badge";
import { DebugSandboxTrigger } from "@/components/workspace/debug-sandbox-trigger";
import { ExportTrigger } from "@/components/workspace/export-trigger";
import { GoalStatus } from "@/components/workspace/goal-status";
import {
  InputBox,
  type InputBoxSubmitOptions,
} from "@/components/workspace/input-box";
import { InternalizeTrigger } from "@/components/workspace/internalize-trigger";
import { KnowledgeScopeSelector } from "@/components/workspace/knowledge-scope-selector";
import {
  MessageList,
  MESSAGE_LIST_DEFAULT_PADDING_BOTTOM,
} from "@/components/workspace/messages";
import { ThreadContext } from "@/components/workspace/messages/context";
import { HumanInputCard } from "@/components/workspace/messages/human-input-card";
import { ProjectAffiliationBadge } from "@/components/workspace/projects/project-affiliation-badge";
import {
  SidecarProvider,
  SidecarTrigger,
} from "@/components/workspace/sidecar";
import { ThreadArchiveStatus } from "@/components/workspace/thread-archive-status";
import { ThreadBackgroundTasks } from "@/components/workspace/thread-background-tasks";
import { ThreadExtensionActions } from "@/components/workspace/thread-extension-actions";
import { ThreadScheduledTasksButton } from "@/components/workspace/thread-scheduled-tasks-button";
import { ThreadSubagentBatches } from "@/components/workspace/thread-subagent-batches";
import { ThreadTitle } from "@/components/workspace/thread-title";
import { TodoList } from "@/components/workspace/todo-list";
import { TokenUsageIndicator } from "@/components/workspace/token-usage-indicator";
import { useActiveGoal } from "@/components/workspace/use-active-goal";
import { Welcome } from "@/components/workspace/welcome";
import { WorkspaceSidebarTrigger } from "@/components/workspace/workspace-sidebar-trigger";
import { useAuth } from "@/core/auth/AuthProvider";
import { hasPermission, PERMISSIONS } from "@/core/auth/permissions";
import {
  useBrowserControlEnabled,
  useKnowledgeBaseEnabled,
} from "@/core/features";
import { useI18n } from "@/core/i18n/hooks";
import {
  ALL_KNOWLEDGE_SCOPE,
  buildKnowledgeScopeSnapshot,
  KNOWLEDGE_SCOPE_KEY,
  type KnowledgeScopeSelection,
} from "@/core/knowledge";
import {
  buildHumanInputResponseText,
  hasOpenHumanInputRequest,
  type HumanInputRequest,
  type HumanInputResponse,
} from "@/core/messages/human-input";
import { isHiddenFromUIMessage } from "@/core/messages/utils";
import { modelTaskInput } from "@/core/models/control";
import { useModels } from "@/core/models/hooks";
import { useModelControl } from "@/core/models/use-model-control";
import { notificationBody } from "@/core/notification/body";
import { useNotification } from "@/core/notification/hooks";
import { useThreadScheduledTaskEvents } from "@/core/scheduled-tasks/events";
import { useScheduleToolResultRefresh } from "@/core/scheduled-tasks/hooks";
import { useLocalSettings, useThreadSettings } from "@/core/settings";
import { resolveThreadContext, selectThreadModel } from "@/core/settings/store";
import { createThread } from "@/core/threads/api";
import {
  useBranchThread,
  useThreadMetadata,
  useThreadStream,
  useThreadTokenUsage,
} from "@/core/threads/hooks";
import {
  selectContextUsage,
  threadTokenUsageToTokenUsage,
} from "@/core/threads/token-usage";
import { useThreadQueue } from "@/core/threads/use-thread-queue";
import { projectIdOfThread, textOfMessage } from "@/core/threads/utils";
import { env } from "@/env";
import { cn } from "@/lib/utils";

import { ChatBox } from "./chat-box";
import { QueuedMessages } from "./queued-messages";
import { useSpecificChatMode } from "./use-chat-mode";
import { useMarkOpenThreadRead } from "./use-mark-open-thread-read";
import { useProjectNewChat } from "./use-project-new-chat";
import { useThreadChat } from "./use-thread-chat";

export default function ChatPage() {
  const { t, locale } = useI18n();
  const { user } = useAuth();
  const canStopStreaming = hasPermission(user, PERMISSIONS.RUNS_CANCEL);
  const canCreateRuns = hasPermission(user, PERMISSIONS.RUNS_CREATE);
  const router = useRouter();
  const { threadId, setThreadId, isNewThread, setIsNewThread, isMock } =
    useThreadChat();
  // Project-scoped new chat (`/workspace/chats/new?project={id}`): see
  // useProjectNewChat, shared with the agent chat page ([argus patch #122]).
  const { projectParam, ensureProjectThread, submissionEpochRef } =
    useProjectNewChat({ threadId, isNewThread });
  // `isNewThread` tracks whether the backend has the thread yet — gates the
  // SDK's history fetch (see issue #2746).  `isWelcomeMode` is the visual
  // welcome layout (centered input, hero, quick actions); we flip it to false
  // the moment the user submits so the UI animates immediately, even though
  // `isNewThread` stays true until the backend actually creates the thread.
  const [isWelcomeMode, setIsWelcomeMode] = useState(isNewThread);
  const [settings, setSettings] = useThreadSettings(threadId);
  const modelControl = useModelControl(
    `${user?.id ?? "anonymous"}:${threadId}`,
    locale,
  );
  const prepareModelControl = modelControl.prepare;
  const [modelChoiceSummary, setModelChoiceSummary] = useState("");
  useEffect(() => setModelChoiceSummary(""), [threadId, user?.id]);
  const [localSettings, setLocalSettings] = useLocalSettings();
  const {
    queue,
    enqueue: enqueueMessage,
    dequeue: dequeueMessage,
    remove: removeQueuedMessage,
    update: updateQueuedMessage,
  } = useThreadQueue(isNewThread || isMock ? undefined : threadId);
  const { enabled: browserControlEnabled } = useBrowserControlEnabled();
  const { tokenUsageEnabled } = useModels();
  const threadTokenUsage = useThreadTokenUsage(
    isNewThread || isMock ? undefined : threadId,
    { enabled: !isMock },
  );
  const threadMetadata = useThreadMetadata(threadId, {
    enabled: !isNewThread && !isMock,
    isMock,
  });
  // A saved thread that exists on the server is being read while open:
  // clears its unread dot (sidebar and chats list) on every device.
  const markThreadRead = useMarkOpenThreadRead(threadId, {
    enabled: !isNewThread && !isMock && threadMetadata.data != null,
  });
  // Lifecycle lines of schedules created in this chat ("Paused by agent",
  // "Finished"); they stay after the task is deleted.
  const scheduledTaskEvents = useThreadScheduledTaskEvents(threadId, {
    isNewThread,
    enabled: !isMock,
  });
  const branchThread = useBranchThread();
  const backendTokenUsage = threadTokenUsageToTokenUsage(threadTokenUsage.data);
  const contextUsage = selectContextUsage(threadTokenUsage.data);
  const mountedRef = useRef(false);
  useSpecificChatMode();

  useEffect(() => {
    mountedRef.current = true;
  }, []);

  // Keep welcome layout in sync when navigating between threads (sidebar
  // clicks, "new chat" button).  Submitting in /chats/new flips the layout
  // via onSend below — `isNewThread` stays true until onStart, so this effect
  // is harmless during the submit transition.
  useEffect(() => {
    setIsWelcomeMode(isNewThread);
  }, [isNewThread]);

  const { showNotification } = useNotification();
  const { scopeSelectionEnabled } = useKnowledgeBaseEnabled();
  const selectorVisible =
    scopeSelectionEnabled && env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true";
  const [knowledgeScope, setKnowledgeScope] =
    useState<KnowledgeScopeSelection | null>(null);
  const previousConversationRef = useRef({ threadId, isNewThread });

  useEffect(() => {
    setKnowledgeScope((current) => {
      if (!selectorVisible) return null;
      return current ?? ALL_KNOWLEDGE_SCOPE;
    });
  }, [selectorVisible]);

  useEffect(() => {
    const previous = previousConversationRef.current;
    if (
      previous.threadId !== threadId ||
      previous.isNewThread !== isNewThread
    ) {
      const isNewThreadRouteReplacement = previous.isNewThread && !isNewThread;
      if (!isNewThreadRouteReplacement) {
        setKnowledgeScope(selectorVisible ? ALL_KNOWLEDGE_SCOPE : null);
      }
    }
    previousConversationRef.current = { threadId, isNewThread };
  }, [isNewThread, selectorVisible, threadId]);

  const currentKnowledgeScopeSnapshot = useMemo(
    () =>
      selectorVisible && knowledgeScope
        ? buildKnowledgeScopeSnapshot(knowledgeScope)
        : null,
    [knowledgeScope, selectorVisible],
  );

  const {
    thread,
    activeRunId,
    pendingUsageMessages,
    sendMessage,
    regenerateMessage,
    editAndRegenerateMessage,
    isUploading,
    isHistoryLoading,
    hasMoreHistory,
    loadMoreHistory,
  } = useThreadStream({
    threadId: isNewThread ? undefined : threadId,
    displayThreadId: threadId,
    context: settings.context,
    isMock,
    // onSend only animates the UI; do NOT flip `isNewThread` here — the
    // LangGraph SDK eagerly fetches /history the moment it receives a
    // thread id and assumes the thread exists on the backend (issue #2746).
    onSend: () => {
      setIsWelcomeMode(false);
    },
    onStart: (createdThreadId) => {
      // ! Important: Never use next.js router for navigation in this case, otherwise it will cause the thread to re-mount and lose all states. Use native history API instead.
      history.replaceState(null, "", `/workspace/chats/${createdThreadId}`);
      setThreadId(createdThreadId);
      setIsNewThread(false);
    },
    onFinish: (state) => {
      // A run in this thread ended (a send, or a joined scheduled run) while
      // it is open: it has been read.
      markThreadRead();
      if (document.hidden || !document.hasFocus()) {
        const lastMessage = state.messages.at(-1);
        showNotification(state.title, {
          body: notificationBody(
            lastMessage ? textOfMessage(lastMessage) : null,
          ),
        });
      }
    },
  });

  const queueRef = useRef(queue);
  queueRef.current = queue;

  const processQueueNext = useCallback(() => {
    if (thread.isLoading || isUploading) return;
    const nextItem = dequeueMessage();
    if (nextItem) {
      void sendMessage(threadId, nextItem.message, undefined, nextItem.options);
    }
  }, [dequeueMessage, isUploading, sendMessage, thread.isLoading, threadId]);

  useEffect(() => {
    if (!thread.isLoading && !isUploading && queue.length > 0) {
      const timer = setTimeout(() => {
        processQueueNext();
      }, 100);
      return () => clearTimeout(timer);
    }
  }, [isUploading, processQueueNext, queue.length, thread.isLoading]);

  const hasThreadMessages = thread.messages.length > 0;
  // A schedule_task result refreshes the header button and cards at once.
  useScheduleToolResultRefresh(isMock ? null : threadId, thread.messages);

  useEffect(() => {
    if (
      !isNewThread &&
      !isMock &&
      threadMetadata.data === null &&
      !threadMetadata.isLoading &&
      !threadMetadata.isFetching &&
      !isHistoryLoading &&
      !hasMoreHistory &&
      !hasThreadMessages
    ) {
      router.replace("/workspace/chats/new");
    }
  }, [
    hasMoreHistory,
    hasThreadMessages,
    isHistoryLoading,
    isMock,
    isNewThread,
    router,
    threadMetadata.data,
    threadMetadata.isFetching,
    threadMetadata.isLoading,
  ]);

  const handleSubmit = useCallback(
    async (message: PromptInputMessage, options?: InputBoxSubmitOptions) => {
      const submissionEpoch = submissionEpochRef.current;
      setModelChoiceSummary("");
      const choice = await prepareModelControl(message.text);
      const modelInput = modelTaskInput(message.text, choice);
      if (submissionEpochRef.current !== submissionEpoch)
        throw new Error("thread-submission-stale");
      const runModelContext: Record<string, unknown> = {};
      if (choice?.model_name) {
        if (choice.scope === "task") {
          if (settings.context.mode !== "ultra") {
            toast.error(
              locale.startsWith("zh")
                ? "当前未启用子任务，请先启用 Ultra 模式。"
                : "Subtasks are disabled. Enable Ultra mode first.",
            );
            throw new Error("subtasks-disabled");
          }
          runModelContext.subagent_model_name = choice.model_name;
        } else {
          runModelContext.model_name = choice.model_name;
          runModelContext.model_selection_requested = choice.model_name;
        }
      }
      await ensureProjectThread();
      // Conversation switched (or the page unmounted) while the project
      // pre-create was pending: drop the submission. Reject silently — the
      // user has already moved on, so a toast would land on the new
      // conversation — and PromptInput keeps the current composer text.
      if (submissionEpochRef.current !== submissionEpoch) {
        throw new Error("thread-submission-stale");
      }
      if (
        choice?.scope === "conversation" &&
        !modelInput.text &&
        message.files.length === 0 &&
        isNewThread &&
        !projectParam
      ) {
        await createThread(threadId);
        if (submissionEpochRef.current !== submissionEpoch)
          throw new Error("thread-submission-stale");
      }
      if (choice?.model_name) {
        if (choice.scope === "conversation")
          try {
            selectThreadModel(threadId, choice.model_name);
          } catch (error) {
            toast.error(
              locale.startsWith("zh")
                ? "无法在浏览器保存本对话的默认模型，请检查存储权限。"
                : "Unable to save this conversation's default model. Check browser storage permissions.",
            );
            throw error;
          }
        const label =
          choice.candidates.find((item) => item.name === choice.model_name)
            ?.label ?? choice.model_name;
        const scopeLabel = locale.startsWith("zh")
          ? ({
              task: "本次子任务",
              turn: "仅本次主任务",
              conversation: "本对话默认模型",
            } as const)
          : ({
              task: "This subtask",
              turn: "This turn only",
              conversation: "Conversation default",
            } as const);
        setModelChoiceSummary(`${scopeLabel[choice.scope!]} · ${label}`);
      } else setModelChoiceSummary("");
      const scopedOptions = currentKnowledgeScopeSnapshot
        ? {
            ...options,
            additionalKwargs: {
              ...options?.additionalKwargs,
              [KNOWLEDGE_SCOPE_KEY]: currentKnowledgeScopeSnapshot,
            },
          }
        : options;
      if (
        choice?.scope === "turn" &&
        !modelInput.text &&
        message.files.length === 0
      ) {
        toast.info(
          locale.startsWith("zh")
            ? "请补充这次需要处理的任务。"
            : "Please include the task to run with this model.",
        );
        throw new Error("model-task-missing");
      }
      if (
        choice?.scope === "conversation" &&
        !modelInput.text &&
        message.files.length === 0
      ) {
        if (isNewThread) {
          history.replaceState(null, "", `/workspace/chats/${threadId}`);
          setThreadId(threadId);
          setIsNewThread(false);
        }
        options?.onSent?.();
        toast.success(
          locale.startsWith("zh")
            ? "已设置本对话的默认模型。"
            : "Conversation default model saved.",
        );
        return;
      }
      const executionOptions = {
        ...scopedOptions,
        additionalKwargs: {
          ...scopedOptions?.additionalKwargs,
          ...modelInput.additionalKwargs,
        },
      };
      const sendPromise = sendMessage(
        threadId,
        { ...message, text: modelInput.text },
        runModelContext,
        executionOptions,
      );
      if (message.files.length > 0) {
        return sendPromise;
      }
      void sendPromise;
    },
    [
      currentKnowledgeScopeSnapshot,
      sendMessage,
      threadId,
      ensureProjectThread,
      submissionEpochRef,
      prepareModelControl,
      settings.context.mode,
      locale,
      isNewThread,
      projectParam,
      setThreadId,
      setIsNewThread,
    ],
  );
  const handleSubmitHumanInput = useCallback(
    async (request: HumanInputRequest, response: HumanInputResponse) => {
      let sent = false;
      await sendMessage(
        threadId,
        {
          text: buildHumanInputResponseText(request, response),
          files: [],
        },
        undefined,
        {
          additionalKwargs: {
            hide_from_ui: true,
            human_input_response: response,
            ...(currentKnowledgeScopeSnapshot
              ? { [KNOWLEDGE_SCOPE_KEY]: currentKnowledgeScopeSnapshot }
              : {}),
          },
          onSent: () => {
            sent = true;
          },
        },
      );
      return sent;
    },
    [currentKnowledgeScopeSnapshot, sendMessage, threadId],
  );
  const handleStop = useCallback(async () => {
    await thread.stop();
  }, [thread]);
  const handleRegenerate = useCallback(
    (messageId: string, supersededMessageIds: string[]) =>
      regenerateMessage(threadId, messageId, supersededMessageIds),
    [regenerateMessage, threadId],
  );
  const handleEditAndRegenerate = useCallback(
    (messageId: string, replacementText: string) =>
      editAndRegenerateMessage(
        threadId,
        messageId,
        replacementText,
        currentKnowledgeScopeSnapshot
          ? { [KNOWLEDGE_SCOPE_KEY]: currentKnowledgeScopeSnapshot }
          : undefined,
      ),
    [currentKnowledgeScopeSnapshot, editAndRegenerateMessage, threadId],
  );
  const handleBranchTurn = useCallback(
    async (messageId: string, messageIds: string[]) => {
      if (
        isNewThread ||
        isMock ||
        env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true"
      ) {
        return;
      }

      try {
        const response = await branchThread.mutateAsync({
          threadId,
          messageId,
          messageIds,
        });
        toast.success(t.conversation.branchCreated);
        router.push(`/workspace/chats/${response.thread_id}`);
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : t.conversation.branchFailed,
        );
      }
    },
    [branchThread, isMock, isNewThread, router, t, threadId],
  );

  const tokenUsageInlineMode = tokenUsageEnabled
    ? localSettings.tokenUsage.inlineMode
    : "off";
  const hasTodos = (thread.values.todos?.length ?? 0) > 0;
  const browserEnabled = !isNewThread && !isMock && browserControlEnabled;
  const { activeGoal, hasGoal, goalOutcome, setLocalGoal } = useActiveGoal(
    threadId,
    thread.values.goal,
    thread.values.goal_outcome,
    thread.messages,
  );
  const hasOpenHumanInputCard = useMemo(
    () =>
      hasOpenHumanInputRequest(
        thread.messages,
        (message) => !isHiddenFromUIMessage(message),
      ),
    [thread.messages],
  );

  // Project affiliation chip: shown once the materialized thread's metadata
  // carries `deerflow_project_id` (written by the create/move endpoints and
  // exposed here read-only).
  const affiliatedProjectId =
    !isNewThread && !isMock && threadMetadata.data
      ? projectIdOfThread(threadMetadata.data)
      : null;
  // A new project chat shows its project before the first message too.
  const shownProjectId = projectParam ?? affiliatedProjectId;
  // A goal blocks edit-and-rerun server-side; show the pencil locked.
  const editBase =
    !isNewThread &&
    !isMock &&
    env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" &&
    !isUploading &&
    !thread.isLoading &&
    !branchThread.isPending &&
    !hasOpenHumanInputCard;

  return (
    <ThreadContext.Provider value={{ thread, isMock }}>
      <SidecarProvider
        parentThreadId={threadId}
        context={settings.context}
        isMock={isMock}
      >
        <ChatBox threadId={threadId} browserEnabled={browserEnabled}>
          <div className="relative flex size-full min-h-0 justify-between">
            <header
              className={cn(
                "absolute top-0 right-0 left-0 flex h-12 shrink-0 items-center gap-2 px-2 sm:px-4",
                isWelcomeMode
                  ? "bg-background/0 z-40 backdrop-blur-none"
                  : "bg-background/80 z-30 shadow-xs backdrop-blur",
              )}
            >
              {!isMock && <WorkspaceSidebarTrigger className="md:hidden" />}
              <div className="flex min-w-0 flex-1 items-center gap-2 text-sm font-medium">
                <ThreadTitle
                  threadId={threadId}
                  thread={thread}
                  canonicalTitle={threadMetadata.data?.values?.title}
                />
                {!isNewThread &&
                  !isMock &&
                  env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" && (
                    <ThreadArchiveStatus
                      threadId={threadId}
                      metadata={threadMetadata.data?.metadata}
                    />
                  )}
                {shownProjectId && (
                  <ProjectAffiliationBadge projectId={shownProjectId} />
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {!isNewThread &&
                  !isMock &&
                  env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" && (
                    <ThreadBackgroundTasks threadId={threadId} />
                  )}
                {!isNewThread &&
                  !isMock &&
                  env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" && (
                    <ThreadSubagentBatches threadId={threadId} />
                  )}
                {!isNewThread && !isMock && (
                  <ThreadScheduledTasksButton threadId={threadId} />
                )}
                {tokenUsageEnabled ? (
                  <TokenUsageIndicator
                    threadId={isNewThread ? undefined : threadId}
                    backendUsage={backendTokenUsage}
                    contextUsage={contextUsage}
                    enabled={tokenUsageEnabled}
                    messages={thread.messages}
                    pendingMessages={pendingUsageMessages}
                    preferences={localSettings.tokenUsage}
                    onPreferencesChange={(preferences) =>
                      setLocalSettings("tokenUsage", preferences)
                    }
                  />
                ) : (
                  <ContextUsageBadge contextUsage={contextUsage} />
                )}
                <SidecarTrigger />
                {browserEnabled && <BrowserTrigger />}
                <DebugSandboxTrigger threadId={threadId} />
                <ExportTrigger threadId={threadId} />
                {!isNewThread && !isMock && (
                  <InternalizeTrigger threadId={threadId} />
                )}
                <ThreadExtensionActions threadId={threadId} />
                <ArtifactTrigger />
              </div>
            </header>
            <main className="flex min-h-0 max-w-full grow flex-col">
              <div className="flex min-h-0 flex-1 justify-center">
                <MessageList
                  archiveDownloadsEnabled={
                    isNewThread || isMock || threadMetadata.data != null
                  }
                  className={cn("size-full", !isWelcomeMode && "pt-10")}
                  testId="main-message-list"
                  threadId={threadId}
                  thread={thread}
                  activeRunId={activeRunId}
                  scheduledTaskEvents={scheduledTaskEvents.data}
                  enableConversationOutline
                  paddingBottom={MESSAGE_LIST_DEFAULT_PADDING_BOTTOM}
                  hasMoreHistory={hasMoreHistory}
                  loadMoreHistory={loadMoreHistory}
                  isHistoryLoading={isHistoryLoading}
                  tokenUsageInlineMode={tokenUsageInlineMode}
                  canRegenerate={
                    !isNewThread &&
                    !isMock &&
                    env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" &&
                    !isUploading &&
                    !thread.isLoading
                  }
                  onRegenerateMessage={handleRegenerate}
                  canEdit={editBase && !hasGoal}
                  editLockedByGoal={editBase && hasGoal}
                  onEditAndRegenerateMessage={handleEditAndRegenerate}
                  onSubmitHumanInput={
                    isMock || env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true"
                      ? undefined
                      : handleSubmitHumanInput
                  }
                  canBranch={
                    !isNewThread &&
                    !isMock &&
                    env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" &&
                    !isUploading &&
                    !thread.isLoading &&
                    !branchThread.isPending
                  }
                  onBranchTurn={handleBranchTurn}
                />
              </div>
              <div
                className={cn(
                  "right-0 bottom-0 left-0 z-30 flex justify-center px-3 sm:px-4",
                  isWelcomeMode ? "absolute" : "relative shrink-0 pb-4",
                )}
              >
                <div
                  className={cn(
                    "relative w-full",
                    isWelcomeMode &&
                      "-translate-y-[calc(50vh-48px)] sm:-translate-y-[calc(50vh-96px)]",
                    isWelcomeMode
                      ? "max-w-(--container-width-sm)"
                      : "max-w-(--container-width-md)",
                  )}
                >
                  {(hasGoal || goalOutcome !== null || hasTodos) && (
                    <div
                      className={cn(
                        "right-0 left-0 z-0",
                        isWelcomeMode ? "absolute -top-4" : "relative",
                      )}
                    >
                      <div
                        className={cn(
                          "right-0 bottom-0 left-0 flex flex-col",
                          isWelcomeMode ? "absolute" : "relative",
                        )}
                      >
                        <GoalStatus
                          goal={activeGoal}
                          outcome={goalOutcome}
                          isRunning={thread.isLoading}
                          hasOpenHumanInputCard={hasOpenHumanInputCard}
                        />
                        {hasTodos && (
                          <TodoList
                            className="bg-background/5"
                            todos={thread.values.todos ?? []}
                            hidden={false}
                          />
                        )}
                      </div>
                    </div>
                  )}
                  {modelControl.request && (
                    <div className="mb-3" data-testid="model-selection-card">
                      <HumanInputCard
                        request={modelControl.request}
                        onSubmit={(response) => modelControl.choose(response)}
                      />
                    </div>
                  )}
                  {modelChoiceSummary && (
                    <p
                      className="text-muted-foreground mb-2 text-sm"
                      data-testid="model-selection-applied"
                    >
                      {modelChoiceSummary}
                    </p>
                  )}
                  {mountedRef.current ? (
                    <>
                      <QueuedMessages
                        queue={queue}
                        onRemove={removeQueuedMessage}
                        onUpdate={updateQueuedMessage}
                      />
                      <InputBox
                        className={cn(
                          "bg-background/5 w-full",
                          isWelcomeMode && "-translate-y-2 sm:-translate-y-4",
                        )}
                        isWelcomeMode={isWelcomeMode}
                        threadId={threadId}
                        projectId={shownProjectId}
                        draftThreadId={isNewThread ? "new" : threadId}
                        knowledgeScopeControl={
                          selectorVisible && knowledgeScope ? (
                            <KnowledgeScopeSelector
                              agentName="lead_agent"
                              disabled={thread.isLoading || isUploading}
                              selection={knowledgeScope}
                              onChange={setKnowledgeScope}
                            />
                          ) : undefined
                        }
                        autoFocus={isWelcomeMode}
                        status={
                          thread.error
                            ? "error"
                            : thread.isLoading
                              ? "streaming"
                              : "ready"
                        }
                        context={settings.context}
                        extraHeader={
                          isWelcomeMode &&
                          !hasGoal &&
                          !hasTodos && <Welcome mode={settings.context.mode} />
                        }
                        disabled={
                          isMock ||
                          env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" ||
                          isUploading ||
                          modelControl.busy ||
                          (!isNewThread && isHistoryLoading)
                        }
                        onContextChange={(context, options) => {
                          if (options?.automatic)
                            resolveThreadContext(threadId, context);
                          else setSettings("context", context);
                        }}
                        onGoalChange={setLocalGoal}
                        onPrepareThread={ensureProjectThread}
                        onReferenceFileAttached={() => {
                          if (!isNewThread) return;
                          history.replaceState(
                            null,
                            "",
                            `/workspace/chats/${threadId}`,
                          );
                          setThreadId(threadId);
                          setIsNewThread(false);
                        }}
                        onSubmit={handleSubmit}
                        onQueue={(msg, opts) => enqueueMessage(msg, opts)}
                        onStop={handleStop}
                        canStopStreaming={canStopStreaming}
                        canCreateRuns={canCreateRuns}
                      />
                    </>
                  ) : (
                    <div
                      aria-hidden="true"
                      className={cn(
                        "bg-background/5 h-32 w-full rounded-2xl",
                        isWelcomeMode && "-translate-y-2 sm:-translate-y-4",
                      )}
                    />
                  )}
                  {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" && (
                    <div className="text-muted-foreground/67 w-full translate-y-12 text-center text-xs">
                      {t.common.notAvailableInDemoMode}
                    </div>
                  )}
                </div>
              </div>
            </main>
          </div>
        </ChatBox>
      </SidecarProvider>
    </ThreadContext.Provider>
  );
}
