"use client";

import { useQuery } from "@tanstack/react-query";
import {
  FolderIcon,
  KeyboardIcon,
  MessageSquarePlusIcon,
  SettingsIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { frontendExtensionsQueryKey } from "@/core/extensions/api";
import {
  loadExtensionCommands,
  type CommandGroupItem,
} from "@/core/extensions/commands";
import { useFrontendExtensions } from "@/core/extensions/hooks";
import { extensionIcon } from "@/core/extensions/registry";
import { useI18n } from "@/core/i18n/hooks";
import { useProjects } from "@/core/projects";
import { useGlobalShortcuts } from "@/hooks/use-global-shortcuts";

import {
  setCommandPaletteOpen,
  useCommandPaletteOpen,
  useMetaKeyLabel,
} from "./command-palette-store";
import { useSettingsDialog } from "./settings";

/** [argus patch #167] cmdk selects by value, so values must be unique; they
 * also take part in matching, so they carry the label, not an opaque id. */
function uniqueValues() {
  const seen = new Map<string, number>();
  return (value: string) => {
    const count = (seen.get(value) ?? 0) + 1;
    seen.set(value, count);
    return count === 1 ? value : `${value} ${count}`;
  };
}

export function CommandPalette() {
  const { t, locale } = useI18n();
  const router = useRouter();
  const { openSettings } = useSettingsDialog();
  const open = useCommandPaletteOpen();
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  // [argus patch #167] Projects and extension groups (Atlas: apps, company
  // tools) load when the palette first opens.
  const { data: projects } = useProjects("active", { enabled: open });
  const extensions = useFrontendExtensions();
  const commands = useQuery({
    queryKey: [...frontendExtensionsQueryKey, "commands", locale],
    queryFn: ({ signal }) =>
      loadExtensionCommands(extensions.data ?? [], { locale, signal }),
    enabled: open && !!extensions.data,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const handleNewChat = useCallback(() => {
    router.push("/workspace/chats/new");
    setCommandPaletteOpen(false);
  }, [router]);

  const handleOpenSettings = useCallback(() => {
    setCommandPaletteOpen(false);
    openSettings("appearance");
  }, [openSettings]);

  const handleShowShortcuts = useCallback(() => {
    setCommandPaletteOpen(false);
    setShortcutsOpen(true);
  }, []);

  const handleOpenHref = useCallback(
    (item: Pick<CommandGroupItem, "href" | "kind">) => {
      setCommandPaletteOpen(false);
      if (item.kind === "internal") router.push(item.href);
      else window.open(item.href, "_blank", "noopener,noreferrer");
    },
    [router],
  );

  const shortcuts = useMemo(
    () => [
      { key: "k", meta: true, action: () => setCommandPaletteOpen((o) => !o) },
      { key: "n", meta: true, shift: true, action: handleNewChat },
      { key: ",", meta: true, action: handleOpenSettings },
      { key: "/", meta: true, action: handleShowShortcuts },
    ],
    [handleNewChat, handleOpenSettings, handleShowShortcuts],
  );

  useGlobalShortcuts(shortcuts);

  const metaKey = useMetaKeyLabel();
  const shiftKey = metaKey === "⌘" ? "⇧" : "Shift+";
  const valueOf = uniqueValues();

  return (
    <>
      <CommandDialog open={open} onOpenChange={setCommandPaletteOpen}>
        <CommandInput placeholder={t.shortcuts.searchActions} />
        <CommandList>
          <CommandEmpty>{t.shortcuts.noResults}</CommandEmpty>
          <CommandGroup heading={t.shortcuts.actions}>
            <CommandItem onSelect={handleNewChat}>
              <MessageSquarePlusIcon className="mr-2 h-4 w-4" />
              {t.sidebar.newChat}
              <CommandShortcut>
                {metaKey}
                {shiftKey}N
              </CommandShortcut>
            </CommandItem>
            <CommandItem onSelect={handleOpenSettings}>
              <SettingsIcon className="mr-2 h-4 w-4" />
              {t.common.settings}
              <CommandShortcut>{metaKey},</CommandShortcut>
            </CommandItem>
            <CommandItem onSelect={handleShowShortcuts}>
              <KeyboardIcon className="mr-2 h-4 w-4" />
              {t.shortcuts.keyboardShortcuts}
              <CommandShortcut>{metaKey}/</CommandShortcut>
            </CommandItem>
          </CommandGroup>
          {!!projects?.length && (
            <CommandGroup heading={t.shortcuts.projects}>
              {projects.map((project) => {
                const slug = project.presentation?.argus_slug;
                return (
                  <CommandItem
                    key={project.id}
                    value={valueOf(project.name)}
                    keywords={typeof slug === "string" ? [slug] : []}
                    onSelect={() =>
                      handleOpenHref({
                        kind: "internal",
                        href: `/workspace/projects/${encodeURIComponent(project.id)}`,
                      })
                    }
                  >
                    <FolderIcon className="mr-2 h-4 w-4" />
                    <span className="truncate">{project.name}</span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          )}
          {commands.data?.groups.map((group) => {
            const Icon = extensionIcon(group.icon);
            return (
              <CommandGroup key={group.key} heading={group.label}>
                {group.items.map((item) => (
                  <CommandItem
                    key={item.id}
                    value={valueOf(
                      [item.label, item.description].filter(Boolean).join(" "),
                    )}
                    keywords={[...(item.keywords ?? [])]}
                    onSelect={() => handleOpenHref(item)}
                  >
                    <Icon className="mr-2 h-4 w-4" />
                    <span className="truncate">{item.label}</span>
                    {item.description && (
                      <span className="text-muted-foreground ml-auto max-w-[45%] truncate pl-3 text-xs">
                        {item.description}
                      </span>
                    )}
                  </CommandItem>
                ))}
              </CommandGroup>
            );
          })}
          {open && commands.isFetching && !commands.data && (
            <p className="text-muted-foreground px-4 py-2 text-xs">
              {t.shortcuts.loadingCommands}
            </p>
          )}
          {(commands.data?.failed ?? commands.isError) && (
            <p className="text-muted-foreground px-4 py-2 text-xs">
              {t.shortcuts.commandsUnavailable}
            </p>
          )}
        </CommandList>
      </CommandDialog>

      <Dialog open={shortcutsOpen} onOpenChange={setShortcutsOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t.shortcuts.keyboardShortcuts}</DialogTitle>
            <DialogDescription>
              {t.shortcuts.keyboardShortcutsDescription}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            {[
              { keys: `${metaKey}K`, label: t.shortcuts.openCommandPalette },
              { keys: `${metaKey}${shiftKey}N`, label: t.sidebar.newChat },
              { keys: `${metaKey}B`, label: t.shortcuts.toggleSidebar },
              { keys: `${metaKey},`, label: t.common.settings },
              {
                keys: `${metaKey}/`,
                label: t.shortcuts.keyboardShortcuts,
              },
            ].map(({ keys, label }) => (
              <div key={keys} className="flex items-center justify-between">
                <span className="text-muted-foreground">{label}</span>
                <kbd className="bg-muted text-muted-foreground rounded px-2 py-0.5 font-mono text-xs">
                  {keys}
                </kbd>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
