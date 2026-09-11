import type { BaseStream } from "@langchain/langgraph-sdk";
import { useEffect } from "react";

import { useI18n } from "@/core/i18n/hooks";
import type { AgentThreadState } from "@/core/threads";
import { cn } from "@/lib/utils";

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

export type ThreadTitleProps = {
  className?: string;
  threadId: string;
  thread: BaseStream<AgentThreadState>;
  canonicalTitle?: string;
};

export function ThreadTitle({
  className,
  threadId,
  thread,
  canonicalTitle,
}: ThreadTitleProps) {
  const { t } = useI18n();
  const { isNewThread } = useThreadChat();
  const title = canonicalTitle?.length ? canonicalTitle : thread.values?.title;

  useEffect(() => {
    let _title = t.pages.untitled;

    if (title) {
      _title = title;
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
    title,
  ]);

  if (!title) {
    return null;
  }
  return (
    <FlipDisplay
      uniqueKey={threadId}
      className={cn("min-w-0 [&>div]:truncate", className)}
    >
      {title}
    </FlipDisplay>
  );
}
