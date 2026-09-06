"use client";

import { MessageSquarePlus, StarIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";
import { env } from "@/env";
import { cn } from "@/lib/utils";

export function WorkspaceHeader({ className }: { className?: string }) {
  const { t } = useI18n();
  const { state } = useSidebar();
  const pathname = usePathname();
  return (
    <>
      <div
        className={cn(
          "group/workspace-header flex h-12 flex-col justify-center",
          className,
        )}
      >
        {state === "collapsed" ? (
          <div className="group-has-data-[collapsible=icon]/sidebar-wrapper:-translate-y flex w-full cursor-pointer items-center justify-center">
            <div
              aria-label="Atlas"
              className="text-primary flex items-center justify-center pt-1 group-hover/workspace-header:hidden"
              data-testid="brand-mark"
              role="img"
            >
              <StarIcon
                aria-hidden="true"
                className="size-4"
                fill="currentColor"
              />
            </div>
            <SidebarTrigger className="hidden pl-2 group-hover/workspace-header:block" />
          </div>
        ) : (
          <div className="flex items-center justify-between gap-2">
            {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" ? (
              <Link
                href="/"
                className="text-primary ml-2 flex items-center gap-2 font-serif"
              >
                <StarIcon
                  aria-hidden="true"
                  className="size-4"
                  fill="currentColor"
                />
                DeerFlow
              </Link>
            ) : (
              <div
                className="text-primary ml-2 flex cursor-default items-center gap-2 font-serif"
                data-testid="brand-mark"
              >
                <StarIcon
                  aria-hidden="true"
                  className="size-4"
                  fill="currentColor"
                />
                DeerFlow
              </div>
            )}
            <SidebarTrigger />
          </div>
        )}
      </div>
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname === "/workspace/chats/new"}
            asChild
          >
            <Link className="text-muted-foreground" href="/workspace/chats/new">
              <MessageSquarePlus size={16} />
              <span>{t.sidebar.newChat}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </>
  );
}
