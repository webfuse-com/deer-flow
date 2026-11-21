import { MonitorIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";
import { useI18n } from "@/core/i18n/hooks";

import { useMaybeSidecar } from "../sidecar/context";

import { useMaybeBrowserView } from "./context";

export const BrowserTrigger = () => {
  const { t } = useI18n();
  const browserView = useMaybeBrowserView();
  const sidecar = useMaybeSidecar();

  // [argus patch #168] Only once the agent has browsed in this chat, to reopen
  // the panel on its screenshot. The manual live browser behind an empty
  // panel never connected on Argus (every stream request 403 in Oct 2026).
  if (!browserView?.latestFrame) {
    return null;
  }
  const browserVisible = browserView.open && !sidecar?.open;
  const label = browserVisible ? t.common.close : t.common.showBrowser;

  return (
    <Tooltip content={label}>
      <Button
        aria-label={label}
        className="text-muted-foreground hover:text-foreground"
        data-testid="browser-trigger"
        size="icon"
        type="button"
        variant={browserVisible ? "secondary" : "ghost"}
        onClick={() => {
          if (browserVisible) {
            browserView.close();
            return;
          }
          sidecar?.close();
          browserView.openPanel();
        }}
      >
        <MonitorIcon />
      </Button>
    </Tooltip>
  );
};
