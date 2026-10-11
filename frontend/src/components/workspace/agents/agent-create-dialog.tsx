"use client";

// [argus patch #110] Create a custom agent from the gallery. Patch #79 had
// removed in-UI creation because agents lived only in the stack's GitHub
// repository; the agent store now commits every create there, so the form
// comes back as a single dialog (the old /workspace/agents/new wizard URL
// still redirects to the gallery).

import { useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { CodeEditor } from "@/components/workspace/code-editor";
import {
  AgentNameCheckError,
  AgentsApiDisabledError,
  checkAgentName,
  useCreateAgent,
} from "@/core/agents";
import { useI18n } from "@/core/i18n/hooks";
import { useModels } from "@/core/models/hooks";

import { DEFAULT_MODEL_VALUE } from "./agent-settings-dialog-helpers";

const AGENT_NAME_RE = /^[A-Za-z0-9-]+$/;

interface AgentCreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AgentCreateDialog({
  open,
  onOpenChange,
}: AgentCreateDialogProps) {
  const { t } = useI18n();
  const { models } = useModels();
  const createAgent = useCreateAgent();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // ``null`` follows the effective default (the first model), so a store that
  // requires an explicit model gets one without the user picking it.
  const [modelChoice, setModelChoice] = useState<string | null>(null);
  const [soul, setSoul] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const model = modelChoice ?? models[0]?.name ?? DEFAULT_MODEL_VALUE;
  const trimmedName = name.trim();
  const nameInvalid = trimmedName !== "" && !AGENT_NAME_RE.test(trimmedName);
  const busy = checking || createAgent.isPending;

  function reset() {
    setName("");
    setDescription("");
    setModelChoice(null);
    setSoul("");
    setNameError(null);
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  function nameCheckErrorText(err: unknown): string {
    if (err instanceof AgentsApiDisabledError) {
      return t.agents.nameStepApiDisabledError;
    }
    if (
      err instanceof AgentNameCheckError &&
      err.reason === "request_failed" &&
      err.detail
    ) {
      return t.agents.nameStepCheckErrorWithDetail.replace(
        "{detail}",
        () => err.detail ?? "",
      );
    }
    return t.agents.createNameCheckError;
  }

  async function handleCreate() {
    if (busy) return;
    if (!AGENT_NAME_RE.test(trimmedName)) {
      setNameError(t.agents.createNameInvalid);
      return;
    }
    setNameError(null);
    setChecking(true);
    try {
      const result = await checkAgentName(trimmedName);
      if (!result.available) {
        setNameError(t.agents.nameStepAlreadyExistsError);
        return;
      }
    } catch (err) {
      setNameError(nameCheckErrorText(err));
      return;
    } finally {
      setChecking(false);
    }

    try {
      const created = await createAgent.mutateAsync({
        name: trimmedName.toLowerCase(),
        description: description.trim(),
        model: model === DEFAULT_MODEL_VALUE ? null : model,
        soul,
      });
      toast.success(
        created?.source?.kind === "github"
          ? t.agents.createdOnGithub
          : t.agents.agentCreated,
      );
      close();
    } catch (err) {
      // The store's refusal (e.g. "config.yaml needs a model", "Not saved to
      // GitHub: ...") is the message.
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          close();
        } else {
          onOpenChange(true);
        }
      }}
    >
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden sm:max-w-2xl"
        data-testid="agent-create-dialog"
      >
        <DialogHeader className="pr-6">
          <DialogTitle>{t.agents.createTitle}</DialogTitle>
          <DialogDescription>{t.agents.createDescription}</DialogDescription>
        </DialogHeader>

        <div className="min-h-0 min-w-0 space-y-4 overflow-y-auto overscroll-contain px-1 py-1">
          <div className="space-y-1.5">
            <label htmlFor="agent-create-name" className="text-sm font-medium">
              {t.agents.createName}
            </label>
            <Input
              id="agent-create-name"
              value={name}
              autoFocus
              autoComplete="off"
              placeholder={t.agents.nameStepPlaceholder}
              aria-invalid={nameInvalid || nameError !== null}
              aria-describedby="agent-create-name-hint"
              onChange={(event) => {
                setName(event.target.value);
                setNameError(null);
              }}
            />
            <p
              id="agent-create-name-hint"
              className={
                nameInvalid || nameError
                  ? "text-destructive text-xs"
                  : "text-muted-foreground text-xs"
              }
              role={nameInvalid || nameError ? "alert" : undefined}
            >
              {nameError ??
                (nameInvalid
                  ? t.agents.createNameInvalid
                  : t.agents.createNameHint)}
            </p>
          </div>

          <div className="space-y-1.5">
            <label
              htmlFor="agent-create-description"
              className="text-sm font-medium"
            >
              {t.agents.settingsAgentDescription}
            </label>
            <Textarea
              id="agent-create-description"
              value={description}
              rows={2}
              placeholder={t.agents.settingsAgentDescriptionPlaceholder}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <span id="agent-create-model-label" className="text-sm font-medium">
              {t.agents.settingsModel}
            </span>
            <Select value={model} onValueChange={setModelChoice}>
              <SelectTrigger
                className="w-full"
                aria-labelledby="agent-create-model-label"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_MODEL_VALUE}>
                  {t.agents.settingsModelDefault}
                </SelectItem>
                {models.map((m) => (
                  <SelectItem key={m.name} value={m.name}>
                    {m.display_name || m.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <span className="text-sm font-medium">{t.agents.settingsSoul}</span>
            <p className="text-muted-foreground text-xs">
              {t.agents.settingsSoulHint}
            </p>
            <div className="h-64 overflow-hidden rounded-md border">
              <CodeEditor
                className="size-full resize-none rounded-none border-none"
                value={soul}
                language="markdown"
                disabled={busy}
                onChange={setSoul}
              />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={busy}>
            {t.common.cancel}
          </Button>
          <Button
            onClick={() => void handleCreate()}
            disabled={busy || trimmedName === "" || nameInvalid}
          >
            {busy ? t.common.loading : t.agents.createSubmit}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
