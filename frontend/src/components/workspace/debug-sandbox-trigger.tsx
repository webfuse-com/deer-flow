"use client";

import { Bug } from "lucide-react";
import { useCallback, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { acquireDebugSandbox } from "@/core/threads/api";

import { Tooltip } from "./tooltip";

/**
 * Header action that opens the current thread's AIO sandbox UI (terminal /
 * code-server / VNC / jupyter) in a new tab.
 *
 * The thread's sandbox container is ephemeral, so the backend acquires (or
 * re-acquires) it on demand and returns a relative `/debug-sandbox/<hash>/`
 * URL served by the per-project nginx behind the same auth boundary as this
 * app. We open the tab only after the acquire resolves — opening eagerly and
 * navigating later is blocked by popup blockers, and opening before the
 * container is up would race the proxy to a 502.
 *
 * The button hides itself when there is no thread yet (welcome mode) and when
 * the deployment has no container-backed sandbox provider (the endpoint
 * returns 409 → `acquireDebugSandbox` resolves to null).
 */
export function DebugSandboxTrigger({
  threadId,
}: {
  threadId: string | undefined;
}) {
  const { t } = useI18n();
  const [loading, setLoading] = useState(false);
  // Latches to true only after a 409, so the affordance disappears on
  // deployments without a container sandbox instead of erroring every click.
  const [unavailable, setUnavailable] = useState(false);

  const handleOpen = useCallback(async () => {
    if (!threadId || loading) {
      return;
    }
    setLoading(true);
    try {
      const result = await acquireDebugSandbox(threadId);
      if (result === null) {
        setUnavailable(true);
        return;
      }
      window.open(result.url, "_blank", "noopener,noreferrer");
    } catch {
      toast.error(t.common.debugSandboxError);
    } finally {
      setLoading(false);
    }
  }, [threadId, loading, t]);

  if (!threadId || unavailable) {
    return null;
  }

  return (
    <Tooltip content={t.common.debugSandbox}>
      <Button
        aria-label={t.common.debugSandbox}
        className="text-muted-foreground hover:text-foreground"
        size="icon"
        type="button"
        variant="ghost"
        disabled={loading}
        onClick={handleOpen}
      >
        <Bug />
      </Button>
    </Tooltip>
  );
}
