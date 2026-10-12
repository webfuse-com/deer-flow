"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { resolveConversationActions } from "@/core/extensions/actions";
import type {
  ConversationAction,
  ConversationActionContext,
  FrontendContribution,
} from "@/core/extensions/contracts";
import {
  useFrontendServices,
  useFrontendExtensions,
} from "@/core/extensions/hooks";
import {
  extensionIcon,
  activeFrontendExtensions,
} from "@/core/extensions/registry";
import { bindFrontendServices } from "@/core/extensions/services";
import { useI18n } from "@/core/i18n/hooks";

import { Tooltip } from "./tooltip";

/** Shared host slot used by the chat toolbar AND every sidebar conversation. */
export function ConversationExtensionActions({
  context,
  placement = "toolbar",
}: {
  context: ConversationActionContext;
  placement?: "toolbar" | "menu";
}) {
  const { t, locale } = useI18n();
  const query = useFrontendExtensions();
  const services = useFrontendServices();
  const [busy, setBusy] = useState(false);
  const entries = query.isError ? [] : (query.data ?? []);

  async function execute(
    action: ConversationAction,
    contribution: FrontendContribution,
  ) {
    setBusy(true);
    try {
      await action.execute(
        context,
        bindFrontendServices(services, contribution),
      );
    } catch {
      toast.error(t.extensions.actionFailed);
    } finally {
      setBusy(false);
    }
  }

  return activeFrontendExtensions(entries).map(
    ({ contribution, extension }) => {
      if (context.messages?.length === 0) return null;
      const group = resolveConversationActions(
        extension,
        contribution,
        t,
        locale,
      );
      if (!group) return null;
      const actions = group.actions;
      const GroupIcon = extensionIcon(group.icon);
      const items = actions.map((action) => {
        const Icon = extensionIcon(action.icon);
        return (
          <DropdownMenuItem
            key={action.id}
            disabled={busy}
            onSelect={() => void execute(action, contribution)}
          >
            <Icon className="text-muted-foreground" />
            <span>{action.label}</span>
          </DropdownMenuItem>
        );
      });
      if (placement === "menu")
        return (
          <DropdownMenuSub key={contribution.namespace}>
            <DropdownMenuSubTrigger>
              <GroupIcon className="text-muted-foreground" />
              <span>{group.label}</span>
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>{items}</DropdownMenuSubContent>
          </DropdownMenuSub>
        );
      // [argus patch #121] A group may name one action for the toolbar button
      // to run directly (one click, no menu); its label becomes the tooltip.
      const direct = actions.find(({ id }) => id === group.toolbarAction);
      if (direct)
        return (
          <Tooltip key={contribution.namespace} content={direct.label}>
            <Button
              aria-label={direct.label}
              className="text-muted-foreground hover:text-foreground"
              size="icon"
              variant="ghost"
              disabled={busy}
              onClick={() => void execute(direct, contribution)}
            >
              <GroupIcon />
            </Button>
          </Tooltip>
        );
      return (
        <DropdownMenu key={contribution.namespace}>
          <Tooltip content={group.label}>
            <DropdownMenuTrigger asChild>
              {/* [argus patch #121] Icon only, like the other header
                  buttons; the label stays the tooltip and accessible name. */}
              <Button
                aria-label={group.label}
                className="text-muted-foreground hover:text-foreground"
                size="icon"
                variant="ghost"
                disabled={busy}
              >
                <GroupIcon />
              </Button>
            </DropdownMenuTrigger>
          </Tooltip>
          <DropdownMenuContent align="end">{items}</DropdownMenuContent>
        </DropdownMenu>
      );
    },
  );
}
