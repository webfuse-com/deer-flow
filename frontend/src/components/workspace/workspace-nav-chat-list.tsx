"use client";

import {
  BookOpenTextIcon,
  Clock3Icon,
  LightbulbIcon,
  MessagesSquare,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  SidebarGroup,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";

export function WorkspaceNavChatList() {
  const { t } = useI18n();
  const pathname = usePathname();
  return (
    <SidebarGroup className="pt-1">
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton isActive={pathname === "/workspace/chats"} asChild>
            <Link className="text-muted-foreground" href="/workspace/chats">
              <MessagesSquare />
              <span>{t.sidebar.chats}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem>
          <SidebarMenuButton asChild>
            <a
              className="text-muted-foreground"
              href="https://chronos.acro.surfly.com/jobs"
              target="_blank"
              rel="noopener noreferrer"
            >
              <Clock3Icon className="text-muted-foreground" />
              <span>Chronos</span>
            </a>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem>
          <SidebarMenuButton asChild>
            <a
              className="text-muted-foreground"
              href="https://apps-nicholas.acro.surfly.com/akropolis-handbook/"
              target="_blank"
              rel="noopener noreferrer"
            >
              <BookOpenTextIcon className="text-muted-foreground" />
              <span>Handbook</span>
            </a>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem>
          <SidebarMenuButton asChild>
            <a
              className="text-muted-foreground"
              href="https://apps-nicholas.acro.surfly.com/acropolis-feature-requests/"
              target="_blank"
              rel="noopener noreferrer"
            >
              <LightbulbIcon className="text-muted-foreground" />
              <span>Feature Requests</span>
            </a>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarGroup>
  );
}
