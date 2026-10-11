"use client";

import { BotIcon, PlusIcon, UploadIcon } from "lucide-react";
import { type ChangeEvent, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PluginSurfaces } from "@/components/workspace/plugin-surfaces";
import { useAgents, useImportAgentPackage } from "@/core/agents";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import { AgentCard } from "./agent-card";
import { AgentCreateDialog } from "./agent-create-dialog";
import { useAgentsPluginTabs } from "./agents-plugin-tabs";

// [argus patch #112] The host gallery tab; plugin tabs are namespaced
// `plugin:<namespace>:<id>`, so they can never shadow it.
const GALLERY_TAB = "agents";

export function AgentGallery() {
  const { t } = useI18n();
  const { agents, isLoading } = useAgents();
  const importAgent = useImportAgentPackage();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [packageDocument, setPackageDocument] = useState<unknown>(null);
  const [importName, setImportName] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  // [argus patch #112] Plugins can add tabs after the gallery (`agents-tab`
  // surfaces). Without any the page has no tab strip. A tab whose plugin
  // went away falls back to the gallery.
  const pluginTabs = useAgentsPluginTabs();
  const [tab, setTab] = useState(GALLERY_TAB);
  const activeTab = pluginTabs.some(({ value }) => value === tab)
    ? tab
    : GALLERY_TAB;

  const closeImportDialog = () => {
    setPackageDocument(null);
    setImportName("");
  };

  const handleImportFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const parsed: unknown = JSON.parse(await file.text());
      const sourceName =
        typeof parsed === "object" &&
        parsed !== null &&
        "agent" in parsed &&
        typeof parsed.agent === "object" &&
        parsed.agent !== null &&
        "name" in parsed.agent &&
        typeof parsed.agent.name === "string"
          ? parsed.agent.name
          : "";
      setPackageDocument(parsed);
      setImportName(sourceName);
    } catch {
      toast.error(t.agents.importInvalidFile);
    }
  };

  const handleImport = async () => {
    if (packageDocument === null) return;
    try {
      await importAgent.mutateAsync({
        agentPackage: packageDocument,
        name: importName.trim() || undefined,
      });
      toast.success(t.agents.importSuccess);
      closeImportDialog();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  };

  const galleryContent = isLoading ? (
    <div className="text-muted-foreground flex h-40 items-center justify-center text-sm">
      {t.common.loading}
    </div>
  ) : agents.length === 0 ? (
    <div className="flex h-64 flex-col items-center justify-center gap-3 text-center">
      <div className="bg-muted flex h-14 w-14 items-center justify-center rounded-full">
        <BotIcon className="text-muted-foreground h-7 w-7" />
      </div>
      <div>
        <p className="font-medium">{t.agents.emptyTitle}</p>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.agents.emptyDescription}
        </p>
      </div>
    </div>
  ) : (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {agents.map((agent) => (
        <AgentCard key={agent.name} agent={agent} />
      ))}
    </div>
  );

  return (
    <div className="flex size-full flex-col">
      {/* Page header */}
      <div className="flex items-center justify-between border-b px-6 py-4">
        <div>
          <h1 className="text-xl font-semibold">{t.agents.title}</h1>
          <p className="text-muted-foreground mt-0.5 text-sm">
            {t.agents.description}
          </p>
        </div>
        {/* [argus patch #112] Import and New act on the gallery only. */}
        <div
          className={cn(
            "flex items-center gap-2",
            activeTab !== GALLERY_TAB && "hidden",
          )}
        >
          <input
            ref={fileInputRef}
            className="hidden"
            type="file"
            accept="application/json,.json"
            onChange={handleImportFile}
          />
          <Button
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
          >
            <UploadIcon className="mr-1.5 h-4 w-4" />
            {t.agents.importAgent}
          </Button>
          {/* [argus patch #110] "New Agent" is back (patch #79 had removed
              it): the agent store commits the new agent to the stack's GitHub
              repository. /workspace/agents/new still redirects here. */}
          <Button onClick={() => setCreateOpen(true)}>
            <PlusIcon className="mr-1.5 h-4 w-4" />
            {t.agents.newAgent}
          </Button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {pluginTabs.length === 0 ? (
          galleryContent
        ) : (
          // [argus patch #112] Gallery first, then one tab per plugin
          // surface, mounted like a project tab (no project in the context).
          <Tabs
            value={activeTab}
            onValueChange={setTab}
            className="flex flex-col gap-6"
          >
            <TabsList aria-label={t.agents.title}>
              <TabsTrigger value={GALLERY_TAB}>
                {t.agents.galleryTab}
              </TabsTrigger>
              {pluginTabs.map(({ surface, value }) => (
                <TabsTrigger key={value} value={value}>
                  {surface.title}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value={GALLERY_TAB}>{galleryContent}</TabsContent>
            {pluginTabs.map(({ contribution, surface, value }) => (
              <TabsContent key={value} value={value}>
                <PluginSurfaces
                  slot="agents-tab"
                  namespace={contribution.namespace}
                  surfaceId={surface.id}
                />
              </TabsContent>
            ))}
          </Tabs>
        )}
      </div>

      {/* [argus patch #110] Mounted only while open so the form starts empty. */}
      {createOpen && (
        <AgentCreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      )}

      <Dialog
        open={packageDocument !== null}
        onOpenChange={(open) => !open && closeImportDialog()}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.agents.importTitle}</DialogTitle>
            <DialogDescription>{t.agents.importDescription}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <label htmlFor="agent-import-name" className="text-sm font-medium">
              {t.agents.importName}
            </label>
            <Input
              id="agent-import-name"
              value={importName}
              onChange={(event) => setImportName(event.target.value)}
              placeholder={t.agents.nameStepPlaceholder}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={closeImportDialog}
              disabled={importAgent.isPending}
            >
              {t.common.cancel}
            </Button>
            <Button
              onClick={handleImport}
              disabled={importAgent.isPending || importName.trim().length === 0}
            >
              {importAgent.isPending ? t.common.loading : t.agents.importAgent}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
