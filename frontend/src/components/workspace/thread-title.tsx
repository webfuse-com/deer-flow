import type { BaseStream } from "@langchain/langgraph-sdk";
import { useEffect } from "react";

import { useI18n } from "@/core/i18n/hooks";
import type { AgentThreadState } from "@/core/threads";

import { useThreadChat } from "./chats";
import { FlipDisplay } from "./flip-display";

export function formatThreadDocumentTitle({
  appName,
  isLoading,
  isThreadLoading,
  title,
}: {
  appName: string;
  isLoading: boolean;
  isThreadLoading: boolean;
  title: string;
}) {
  if (isThreadLoading) {
    return `Loading... - ${appName}`;
  }
  if (isLoading) {
    return `🧠 [Running] ${title} - ${appName}`;
  }
  return `${title} - ${appName}`;
}

export function ThreadTitle({
  threadId,
  thread,
}: {
  className?: string;
  threadId: string;
  thread: BaseStream<AgentThreadState>;
}) {
  const { t } = useI18n();
  const { isNewThread } = useThreadChat();
  useEffect(() => {
    let _title = t.pages.untitled;

    if (thread.values?.title) {
      _title = thread.values.title;
    } else if (isNewThread) {
      _title = t.pages.newChat;
    }
    document.title = formatThreadDocumentTitle({
      appName: t.pages.appName,
      isLoading: thread.isLoading,
      isThreadLoading: thread.isThreadLoading,
      title: _title,
    });
  }, [
    isNewThread,
    t.pages.newChat,
    t.pages.untitled,
    t.pages.appName,
    thread.isThreadLoading,
    thread.isLoading,
    thread.values,
  ]);

  if (!thread.values?.title) {
    return null;
  }
  return (
    <FlipDisplay uniqueKey={threadId}>
      {thread.values.title ?? "Untitled"}
    </FlipDisplay>
  );
}
