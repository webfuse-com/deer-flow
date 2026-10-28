"use client";

import { useQuery } from "@tanstack/react-query";
import {
  FolderIcon,
  KeyboardIcon,
  MessageSquarePlusIcon,
  SettingsIcon,
  type LucideIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState, type ReactNode } from "react";

import {
  Command,
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

import { rankCommandGroups } from "./command-palette-search";
import {
  setCommandPaletteOpen,
  useCommandPaletteOpen,
  useMetaKeyLabel,
} from "./command-palette-store";
import { useSettingsDialog } from "./settings";

// [argus patch #167] CommandDialog's styling, composed here because the
// palette turns cmdk's own filter off (command-palette-search.ts ranks).
const COMMAND_CLASSES =
  "[&_[cmdk-group-heading]]:text-muted-foreground **:data-[slot=command-input-wrapper]:h-12 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group]]:px-2 [&_[cmdk-group]:not([hidden])_~[cmdk-group]]:pt-0 [&_[cmdk-input-wrapper]_svg]:h-5 [&_[cmdk-input-wrapper]_svg]:w-5 [&_[cmdk-input]]:h-12 [&_[cmdk-item]]:px-2 [&_[cmdk-item]]:py-3 [&_[cmdk-item]_svg]:h-5 [&_[cmdk-item]_svg]:w-5";

type PaletteItem = {
  key: string;
  label: string;
  description?: string;
  keywords?: readonly string[];
  icon: LucideIcon;
  shortcut?: ReactNode;
  run: () => void;
};
type PaletteGroup = { key: string; heading: string; items: PaletteItem[] };

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
      {
        key: "k",
        meta: true,
        action: () => setCommandPaletteOpen((o) => !o),
      },
      { key: "n", meta: true, shift: true, action: handleNewChat },
      { key: ",", meta: true, action: handleOpenSettings },
      { key: "/", meta: true, action: handleShowShortcuts },
    ],
    [handleNewChat, handleOpenSettings, handleShowShortcuts],
  );

  useGlobalShortcuts(shortcuts);

  const metaKey = useMetaKeyLabel();
  const shiftKey = metaKey === "⌘" ? "⇧" : "Shift+";

  // Default order: extension groups placed at the start (Atlas: Apps), the
  // stack's projects, the other extension groups, then the fixed actions.
  const groups = useMemo<PaletteGroup[]>(() => {
    const extensionGroups = (position: "start" | "end") =>
      (commands.data?.groups ?? [])
        .filter((group) => group.position === position)
        .map((group) => {
          const icon = extensionIcon(group.icon);
          return {
            key: group.key,
            heading: group.label,
            items: group.items.map((item) => ({
              key: `${group.key}:${item.id}`,
              label: item.label,
              description: item.description,
              keywords: item.keywords,
              icon,
              run: () => handleOpenHref(item),
            })),
          };
        });
    return [
      ...extensionGroups("start"),
      {
        key: "projects",
        heading: t.shortcuts.projects,
        items: (projects ?? []).map((project) => {
          const slug = project.presentation?.argus_slug;
          return {
            key: `project:${project.id}`,
            label: project.name,
            keywords: typeof slug === "string" ? [slug] : [],
            icon: FolderIcon,
            run: () =>
              handleOpenHref({
                kind: "internal",
                href: `/workspace/projects/${encodeURIComponent(project.id)}`,
              }),
          };
        }),
      },
      ...extensionGroups("end"),
      {
        key: "actions",
        heading: t.shortcuts.actions,
        items: [
          {
            key: "action:new-chat",
            label: t.sidebar.newChat,
            icon: MessageSquarePlusIcon,
            shortcut: `${metaKey}${shiftKey}N`,
            run: handleNewChat,
          },
          {
            key: "action:settings",
            label: t.common.settings,
            icon: SettingsIcon,
            shortcut: `${metaKey},`,
            run: handleOpenSettings,
          },
          {
            key: "action:shortcuts",
            label: t.shortcuts.keyboardShortcuts,
            icon: KeyboardIcon,
            shortcut: `${metaKey}/`,
            run: handleShowShortcuts,
          },
        ],
      },
    ];
  }, [
    commands.data,
    projects,
    t,
    metaKey,
    shiftKey,
    handleOpenHref,
    handleNewChat,
    handleOpenSettings,
    handleShowShortcuts,
  ]);
  return (
    <>
      <Dialog open={open} onOpenChange={setCommandPaletteOpen}>
        <DialogContent className="overflow-hidden p-0">
          <DialogTitle className="sr-only">
            {t.shortcuts.openCommandPalette}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {t.shortcuts.searchActions}
          </DialogDescription>
          <PaletteSearch
            groups={groups}
            placeholder={t.shortcuts.searchActions}
            empty={t.shortcuts.noResults}
            notice={
              open && commands.isFetching && !commands.data
                ? t.shortcuts.loadingCommands
                : (commands.data?.failed ?? commands.isError)
                  ? t.shortcuts.commandsUnavailable
                  : undefined
            }
          />
        </DialogContent>
      </Dialog>

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

/** Mounted with the dialog, so every open starts from an empty search. */
function PaletteSearch({
  groups,
  placeholder,
  empty,
  notice,
}: {
  groups: PaletteGroup[];
  placeholder: string;
  empty: string;
  notice?: string;
}) {
  const [search, setSearch] = useState("");
  const ranked = useMemo(
    () => rankCommandGroups(groups, search),
    [groups, search],
  );
  return (
    <Command shouldFilter={false} className={COMMAND_CLASSES}>
      <CommandInput
        placeholder={placeholder}
        value={search}
        onValueChange={setSearch}
      />
      <CommandList>
        <CommandEmpty>{empty}</CommandEmpty>
        {ranked.map((group) => (
          <CommandGroup key={group.key} heading={group.heading}>
            {group.items.map((item) => (
              <CommandItem key={item.key} value={item.key} onSelect={item.run}>
                <item.icon className="mr-2 h-4 w-4" />
                <span className="truncate">{item.label}</span>
                {item.shortcut ? (
                  <CommandShortcut>{item.shortcut}</CommandShortcut>
                ) : (
                  item.description && (
                    <span className="text-muted-foreground ml-auto max-w-[45%] truncate pl-3 text-xs">
                      {item.description}
                    </span>
                  )
                )}
              </CommandItem>
            ))}
          </CommandGroup>
        ))}
        {notice && (
          <p className="text-muted-foreground px-4 py-2 text-xs">{notice}</p>
        )}
      </CommandList>
    </Command>
  );
}
