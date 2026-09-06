"use client";

import { CopyIcon, Landmark } from "lucide-react";
import { useEffect, useMemo } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { ArtifactTrigger } from "@/components/workspace/artifacts";
import { ChatBox } from "@/components/workspace/chats/chat-box";
import { ExportTrigger } from "@/components/workspace/export-trigger";
import { MessageList } from "@/components/workspace/messages";
import { ThreadContext } from "@/components/workspace/messages/context";
import { MessageListSkeleton } from "@/components/workspace/messages/skeleton";
import { formatThreadDocumentTitle } from "@/components/workspace/thread-title";
import { Tooltip } from "@/components/workspace/tooltip";
import { writeTextToClipboard } from "@/core/clipboard";
import { useI18n } from "@/core/i18n/hooks";
import {
  SharingRequestError,
  type SharedThreadPayload,
} from "@/core/sharing/api";
import { formatSnapshotDate } from "@/core/sharing/format";
import { useSharedThread } from "@/core/sharing/hooks";
import { snapshotToStream } from "@/core/sharing/snapshot";
import { toSharedThreadId } from "@/core/sharing/thread-id";

const SHARED_MESSAGE_LIST_PADDING_BOTTOM = 24;

function CopyLinkButton({ url }: { url: string }) {
  const { t } = useI18n();
  return (
    <Tooltip content={t.sharing.copyLink}>
      <Button
        aria-label={t.sharing.copyLink}
        className="text-muted-foreground hover:text-foreground"
        size="icon"
        variant="ghost"
        onClick={() => {
          void writeTextToClipboard(url).then((didCopy) => {
            if (didCopy) {
              toast.success(t.sharing.linkCopied);
            } else {
              toast.error(t.clipboard.failedToCopyToClipboard);
            }
          });
        }}
      >
        <CopyIcon />
      </Button>
    </Tooltip>
  );
}

function SharedThreadNotice({ payload }: { payload: SharedThreadPayload }) {
  const { t } = useI18n();
  const parts = [
    t.sharing.readOnlyNotice(formatSnapshotDate(payload.share.snapshot_at)),
  ];
  if (
    !payload.share.include_tool_bodies &&
    payload.snapshot.omitted.tool_bodies > 0
  ) {
    parts.push(t.sharing.toolBodiesOmitted);
  }
  if (!payload.share.files_included) {
    parts.push(t.sharing.filesNotIncluded);
  }
  return (
    <p
      className="text-muted-foreground border-t px-4 py-2 text-center text-xs"
      data-testid="shared-thread-notice"
    >
      {parts.join(" ")}
    </p>
  );
}

function SharedThreadEmpty({
  title,
  description,
  reload,
}: {
  title: string;
  description: string;
  reload?: boolean;
}) {
  const { t } = useI18n();
  return (
    <div className="flex size-full flex-col items-center justify-center gap-3 p-8 text-center">
      <Landmark className="text-muted-foreground size-8" />
      <h2 className="text-lg font-medium">{title}</h2>
      <p className="text-muted-foreground max-w-md text-sm">{description}</p>
      {reload && (
        <Button variant="outline" onClick={() => window.location.reload()}>
          {t.sharing.reload}
        </Button>
      )}
    </div>
  );
}

/**
 * Argus patch #88: a colleague's shared conversation, rendered read-only by
 * the viewer's own frontend from the Agora snapshot. The message components
 * are the same ones the live chat uses; only the data source differs, and
 * every artifact URL is routed to the Agora through the `shared:` thread id.
 */
export function SharedThreadPage({
  stack,
  threadId,
}: {
  stack: string;
  threadId: string;
}) {
  const { t } = useI18n();
  const query = useSharedThread(stack, threadId);
  const sharedThreadId = useMemo(
    () => toSharedThreadId(stack, threadId),
    [stack, threadId],
  );
  const payload = query.data;
  const thread = useMemo(
    () => (payload ? snapshotToStream(payload) : null),
    [payload],
  );

  useEffect(() => {
    if (!payload) {
      return;
    }
    document.title = formatThreadDocumentTitle({
      appName: t.pages.appName,
      isLoading: false,
      isThreadLoading: false,
      title: payload.share.title,
    });
  }, [payload, t.pages.appName]);

  if (query.isPending) {
    return (
      <div className="flex size-full min-h-0 justify-center pt-10">
        <MessageListSkeleton />
      </div>
    );
  }

  if (!payload || !thread) {
    const error = query.error;
    const notFound =
      error instanceof SharingRequestError && error.status === 404;
    const sessionExpired =
      error instanceof SharingRequestError && error.isSessionExpired;
    return (
      <SharedThreadEmpty
        title={
          notFound
            ? t.sharing.notFoundTitle
            : sessionExpired
              ? t.sharing.sessionExpired
              : t.sharing.failed
        }
        description={
          notFound
            ? t.sharing.notFoundDescription
            : (error?.message ?? t.sharing.failed)
        }
        reload={sessionExpired}
      />
    );
  }

  return (
    <ThreadContext.Provider value={{ thread, isMock: false }}>
      <ChatBox threadId={sharedThreadId} browserEnabled={false}>
        <div className="relative flex size-full min-h-0 justify-between">
          <header className="bg-background/80 absolute top-0 right-0 left-0 z-30 flex h-12 shrink-0 items-center gap-2 px-2 shadow-xs backdrop-blur sm:px-4">
            <SidebarTrigger className="md:hidden" />
            <div className="flex min-w-0 flex-1 items-center gap-2 text-sm font-medium">
              <Badge variant="secondary" className="shrink-0">
                <Landmark />
                {t.sharing.sharedBy(payload.share.owner_email)}
              </Badge>
              <span className="truncate" data-testid="shared-thread-title">
                {payload.share.title}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <CopyLinkButton url={payload.share_url} />
              <ExportTrigger threadId={sharedThreadId} />
              <ArtifactTrigger />
            </div>
          </header>
          <main className="flex min-h-0 max-w-full grow flex-col">
            <div className="flex min-h-0 flex-1 justify-center">
              <MessageList
                className="size-full pt-10"
                testId="shared-message-list"
                threadId={sharedThreadId}
                thread={thread}
                paddingBottom={SHARED_MESSAGE_LIST_PADDING_BOTTOM}
                hasMoreHistory={false}
                canRegenerate={false}
                canEdit={false}
                canBranch={false}
                enableSidecarActions={false}
                initialScroll="instant"
              />
            </div>
            <SharedThreadNotice payload={payload} />
          </main>
        </div>
      </ChatBox>
    </ThreadContext.Provider>
  );
}
