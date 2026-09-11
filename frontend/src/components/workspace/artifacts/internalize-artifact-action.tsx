"use client";

import { useContext, useState } from "react";

import { ArtifactAction } from "@/components/ai-elements/artifact";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { useFileShareStatus } from "@/core/sharing/hooks";
import { isSharedThreadId } from "@/core/sharing/thread-id";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

import { ThreadContext } from "../messages/context";
import { ShareStateIcon, shareStateGlyph } from "../share-state-icon";
import { Tooltip } from "../tooltip";

import { InternalizeFileDialog } from "./internalize-file-dialog";

/** Only files the agent produced can be shared; uploads and edits-in-flight cannot. */
const SHAREABLE_PREFIXES = [
  "/mnt/user-data/outputs/",
  "/mnt/user-data/workspace/",
];

export function isShareableArtifactPath(filepath: string): boolean {
  if (filepath.startsWith("write-file:")) {
    return false;
  }
  return SHAREABLE_PREFIXES.some((prefix) => filepath.startsWith(prefix));
}

/**
 * Argus patch #88: share one produced file with every colleague through the
 * Agora. Rendered in the artifact detail toolbar (`variant="detail"`) and on
 * the artifact list card (`variant="card"`).
 */
export function InternalizeArtifactAction({
  threadId,
  filepath,
  variant,
}: {
  threadId: string;
  filepath: string;
  variant: "detail" | "card";
}) {
  const { t } = useI18n();
  // The list card also renders outside a ThreadContext (upstream's archive
  // download surface mounts ArtifactFileList on its own), so read the context
  // optionally instead of through useThread(), which throws without one.
  const isMock = useContext(ThreadContext)?.isMock ?? false;
  const [open, setOpen] = useState(false);

  const eligible =
    !isSharedThreadId(threadId) &&
    !isMock &&
    !isStaticWebsiteOnly() &&
    isShareableArtifactPath(filepath);
  const status = useFileShareStatus(threadId, filepath, { enabled: eligible });

  if (!eligible) {
    return null;
  }

  const shared = status.data?.shared === true;
  const label = shared
    ? t.sharing.artifact.shared
    : t.sharing.artifact.internalize;

  // The dialog renders through a portal, but React events still bubble to the
  // card that owns this action; keep clicks inside the dialog from opening the
  // artifact detail.
  const dialog = (
    <span
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <InternalizeFileDialog
        threadId={threadId}
        filepath={filepath}
        open={open}
        onOpenChange={setOpen}
      />
    </span>
  );

  if (variant === "detail") {
    return (
      <>
        <ArtifactAction
          icon={shareStateGlyph(shared)}
          label={label}
          tooltip={label}
          aria-pressed={shared}
          className={cn(shared && "text-primary")}
          onClick={() => setOpen(true)}
        />
        {dialog}
      </>
    );
  }

  // Icon-only on the card (patch #89): the label lives in the tooltip and the
  // accessible name, so the card's filename keeps its room.
  return (
    <>
      <Tooltip content={label}>
        <Button
          aria-label={label}
          aria-pressed={shared}
          className={cn("size-8", shared && "text-primary")}
          data-testid="internalize-artifact-card"
          size="icon"
          variant="ghost"
          onClick={(event) => {
            event.stopPropagation();
            event.preventDefault();
            setOpen(true);
          }}
        >
          <ShareStateIcon shared={shared} className="size-4" />
        </Button>
      </Tooltip>
      {dialog}
    </>
  );
}
