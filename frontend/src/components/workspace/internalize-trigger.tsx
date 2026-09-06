"use client";

import { Landmark } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { useThreadShareStatus } from "@/core/sharing/hooks";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

import { InternalizeDialog } from "./internalize-dialog";
import { useThread } from "./messages/context";
import { Tooltip } from "./tooltip";

/**
 * Argus patch #88: the "Internalize" control in the thread header. Shares the
 * open conversation with every colleague through the Agora (a stored snapshot
 * the viewer's own Atlas renders). The icon is the temple: to the Agora.
 */
export function InternalizeTrigger({ threadId }: { threadId: string }) {
  const { t } = useI18n();
  const { thread, isMock } = useThread();
  const [open, setOpen] = useState(false);

  const hidden =
    Boolean(isMock) || isStaticWebsiteOnly() || thread.messages.length === 0;
  const status = useThreadShareStatus(threadId, { enabled: !hidden });

  if (hidden) {
    return null;
  }

  const shared = status.data?.shared === true;
  const label = shared ? t.sharing.shared : t.sharing.internalize;

  return (
    <>
      <Tooltip content={label}>
        <Button
          aria-label={label}
          aria-pressed={shared}
          className={cn(
            "hover:text-foreground",
            shared ? "text-primary" : "text-muted-foreground",
          )}
          data-testid="internalize-trigger"
          size="icon"
          variant="ghost"
          onClick={() => setOpen(true)}
        >
          <Landmark />
        </Button>
      </Tooltip>
      <InternalizeDialog
        threadId={threadId}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}
