import { notFound } from "next/navigation";

import { SharedThreadPage } from "@/components/workspace/shared/shared-thread-page";
import { isValidStack, isValidThreadId } from "@/core/sharing/thread-id";

/**
 * A colleague's shared conversation (Argus patch #88). The stack and thread id
 * name the snapshot the Agora serves; the shapes are validated here so a
 * malformed link is a plain 404 before any request leaves the browser.
 */
export default async function WorkspaceSharedThreadPage({
  params,
}: {
  params: Promise<{ stack: string; thread_id: string }>;
}) {
  const { stack, thread_id: threadId } = await params;
  if (!isValidStack(stack) || !isValidThreadId(threadId)) {
    notFound();
  }
  return <SharedThreadPage stack={stack} threadId={threadId} />;
}
