"use client";

import {
  ChevronRightIcon,
  ExternalLinkIcon,
  Maximize2,
  Minimize2,
} from "lucide-react";
import { useId, useMemo, useState } from "react";
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
import { useUpdateAgent } from "@/core/agents";
import type { Agent, ReasoningEffort } from "@/core/agents";
import { useKnowledgeBaseEnabled } from "@/core/features";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildKnowledgeScopeSnapshot,
  knowledgeScopeToSelection,
  type KnowledgeScopeSelection,
} from "@/core/knowledge";
import { useModels } from "@/core/models/hooks";
import {
  getReasoningEffortOptions,
  isThinkingRequired,
  supportsThinking as modelSupportsThinking,
} from "@/core/models/reasoning";
import { useSubagents } from "@/core/subagents";
import { cn } from "@/lib/utils";

import { KnowledgeScopeSelector } from "../knowledge-scope-selector";

import { AgentCapabilitySelection } from "./agent-capability-selection";
import {
  allowedSubagentsToSelection,
  DEFAULT_MODEL_VALUE,
  INHERIT_VALUE,
  MAX_AGENT_OUTPUT_TOKENS,
  parseAgentModelSettingsDraft,
  resolveEffectiveModel,
  selectionToAllowedSubagents,
  selectionToThinkingEnabled,
  type SubagentAccessSelection,
  thinkingEnabledToSelection,
} from "./agent-settings-dialog-helpers";

function sameSelection(left: string[] | null, right: string[] | null) {
  if (left === null || right === null) return left === right;
  const selected = new Set(left);
  return (
    selected.size === new Set(right).size &&
    right.every((id) => selected.has(id))
  );
}

const REASONING_EFFORTS: ReasoningEffort[] = ["low", "medium", "high"];

interface AgentSettingsDialogProps {
  agent: Agent;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Edits a custom agent's display name and model behavior: default model plus the
 * per-agent temperature / max_tokens overrides and thinking / reasoning
 * defaults. Persists through `PUT /api/agents/{name}`; changes take effect on
 * the agent's next run.
 *
 * [argus patch #110] Also edits the description and the soul (SOUL.md), and
 * links to the GitHub copy when the agent store keeps one there: every save
 * is a commit, and a store refusal (conflict, validation, GitHub down) is
 * shown as the server's own text.
 */
export function AgentSettingsDialog({
  agent,
  open,
  onOpenChange,
}: AgentSettingsDialogProps) {
  const { t } = useI18n();
  const {
    models,
    isFetching: modelsFetching,
    refetch: refetchModels,
  } = useModels();
  const { scopeSelectionEnabled } = useKnowledgeBaseEnabled();
  const [knowledgeSelection, setKnowledgeSelection] =
    useState<KnowledgeScopeSelection>(() =>
      knowledgeScopeToSelection(agent.knowledge_scope),
    );
  const [knowledgeChanged, setKnowledgeChanged] = useState(false);
  const { subagents } = useSubagents();
  const subagentDescriptionId = useId();
  const modelStatusId = useId();
  const thinkingHintId = useId();
  const effortHintId = useId();
  const updateAgent = useUpdateAgent();
  // Keep the opening snapshot even if a background refetch updates agent props.
  const [initialSelections] = useState(() => ({
    plugins: agent.mcp_plugins ?? null,
    skills: agent.skills ?? null,
    toolGroups: agent.tool_groups ?? null,
    model: agent.model ?? DEFAULT_MODEL_VALUE,
    thinking: thinkingEnabledToSelection(agent.thinking_enabled),
    reasoningEffort: agent.reasoning_effort ?? INHERIT_VALUE,
  }));
  const [plugins, setPlugins] = useState<string[] | null>(
    agent.mcp_plugins ?? null,
  );
  const [skills, setSkills] = useState<string[] | null>(agent.skills ?? null);
  const [toolGroups, setToolGroups] = useState<string[] | null>(
    agent.tool_groups ?? null,
  );
  const toolGroupsChanged = !sameSelection(
    toolGroups,
    initialSelections.toolGroups,
  );
  const knowledgeAllowed =
    toolGroups === null || toolGroups.includes("knowledge");
  const clearKnowledgeDefault = toolGroupsChanged && !knowledgeAllowed;
  // Catalog reads authorize against saved groups, never the dialog's draft.
  const knowledgeAccessNeedsSave =
    knowledgeAllowed &&
    agent.tool_groups != null &&
    !agent.tool_groups.includes("knowledge");
  const [displayName, setDisplayName] = useState(agent.display_name ?? "");
  // [argus patch #110] Description and soul, sent only when edited so an
  // untouched save never rewrites them (or commits a no-op) on GitHub.
  const [initialText] = useState(() => ({
    description: agent.description ?? "",
    soul: agent.soul ?? "",
  }));
  const [description, setDescription] = useState(initialText.description);
  const [soul, setSoul] = useState(initialText.soul);
  const [soulExpanded, setSoulExpanded] = useState(false);
  const githubSource =
    agent.source?.kind === "github" && agent.source.url ? agent.source : null;

  const [model, setModel] = useState(initialSelections.model);
  const [temperature, setTemperature] = useState(
    agent.model_settings?.temperature != null
      ? String(agent.model_settings.temperature)
      : "",
  );
  const [maxTokens, setMaxTokens] = useState(
    agent.model_settings?.max_tokens != null
      ? String(agent.model_settings.max_tokens)
      : "",
  );
  const [thinking, setThinking] = useState(initialSelections.thinking);
  const [reasoningEffort, setReasoningEffort] = useState(
    initialSelections.reasoningEffort,
  );
  const [subagentAccess, setSubagentAccess] = useState<SubagentAccessSelection>(
    allowedSubagentsToSelection(agent.allowed_subagents),
  );
  const [selectedSubagents, setSelectedSubagents] = useState<string[]>(
    agent.allowed_subagents ?? [],
  );

  // The resolved profile gates which controls are meaningful: thinking and
  // reasoning-effort only apply when the selected model advertises support.
  // When the agent inherits the global default model, fall back to the
  // effective default (models[0]) so the controls are not hidden for it.
  const selectedModel = useMemo(
    () => resolveEffectiveModel(models, model),
    [models, model],
  );
  const modelChanged = model !== initialSelections.model;
  // Missing catalog data does not prove a model lacks reasoning support.
  // Normalize dependent settings only after an intentional, known model switch.
  const modelKnown = selectedModel !== undefined;
  const modelSwitchUnresolved = modelChanged && !modelKnown;
  const normalizeReasoning = modelChanged && modelKnown;
  const supportsThinking = modelSupportsThinking(selectedModel);
  const thinkingRequired = isThinkingRequired(selectedModel);
  // Per-agent defaults keep the generic low/medium/high schema; offer only the
  // values the selected model's reasoning contract also accepts (issue #5073).
  const reasoningEffortOptions = useMemo(
    () =>
      getReasoningEffortOptions(selectedModel).filter(
        (effort): effort is ReasoningEffort =>
          (REASONING_EFFORTS as string[]).includes(effort),
      ),
    [selectedModel],
  );
  const supportsReasoningEffort = reasoningEffortOptions.length > 0;
  const thinkingUnsupported =
    modelKnown &&
    thinking !== INHERIT_VALUE &&
    (!supportsThinking || (thinkingRequired && thinking === "off"));
  const effortUnsupported =
    modelKnown &&
    reasoningEffort !== INHERIT_VALUE &&
    !reasoningEffortOptions.includes(reasoningEffort as ReasoningEffort);
  // Preview normalization only for a deliberate model switch. Otherwise show
  // the saved value, including an incompatible one, so Inherit is actionable.
  const effectiveThinking =
    normalizeReasoning && thinkingUnsupported ? INHERIT_VALUE : thinking;
  const effectiveEffort =
    normalizeReasoning && effortUnsupported ? INHERIT_VALUE : reasoningEffort;
  const selectableSubagents = useMemo(
    () =>
      Array.from(
        new Map(
          subagents
            .filter((item) => item.enabled && !item.conflict)
            .map((item) => [item.name, item]),
        ).values(),
      ),
    [subagents],
  );
  const missingSubagents = useMemo(() => {
    const selectableNames = new Set(
      selectableSubagents.map((item) => item.name),
    );
    return selectedSubagents.filter((name) => !selectableNames.has(name));
  }, [selectableSubagents, selectedSubagents]);

  async function handleSave() {
    if (modelSwitchUnresolved) return;
    if ([...displayName.trim()].length > 100) {
      toast.error(t.agents.settingsDisplayNameTooLong);
      return;
    }
    const parsedSettings = parseAgentModelSettingsDraft({
      temperature,
      maxTokens,
    });
    if (!parsedSettings.ok) {
      toast.error(
        parsedSettings.error === "temperature"
          ? t.agents.settingsInvalidTemperature
          : t.agents.settingsInvalidMaxTokens,
      );
      return;
    }

    try {
      const saved = await updateAgent.mutateAsync({
        name: agent.name,
        request: {
          display_name: displayName.trim() || null,
          // [argus patch #110]
          ...(description.trim() !== initialText.description.trim() && {
            description: description.trim(),
          }),
          ...(soul !== initialText.soul && { soul }),
          ...((knowledgeChanged || clearKnowledgeDefault) && {
            knowledge_scope:
              clearKnowledgeDefault || knowledgeSelection.mode === "all"
                ? null
                : buildKnowledgeScopeSnapshot(knowledgeSelection),
          }),
          ...(!sameSelection(plugins, initialSelections.plugins) && {
            mcp_plugins: plugins,
          }),
          ...(!sameSelection(skills, initialSelections.skills) && { skills }),
          ...(toolGroupsChanged && {
            tool_groups: toolGroups,
          }),
          ...(modelChanged && {
            model: model === DEFAULT_MODEL_VALUE ? null : model,
          }),
          model_settings: parsedSettings.modelSettings,
          ...((thinking !== initialSelections.thinking ||
            normalizeReasoning) && {
            thinking_enabled:
              modelKnown && !supportsThinking
                ? null
                : selectionToThinkingEnabled(effectiveThinking),
          }),
          ...((reasoningEffort !== initialSelections.reasoningEffort ||
            normalizeReasoning) && {
            reasoning_effort:
              reasoningEffort === INHERIT_VALUE ||
              (modelKnown &&
                (!supportsReasoningEffort ||
                  !reasoningEffortOptions.includes(
                    reasoningEffort as ReasoningEffort,
                  )))
                ? null
                : (reasoningEffort as ReasoningEffort),
          }),
          allowed_subagents: selectionToAllowedSubagents(
            subagentAccess,
            selectedSubagents,
          ),
        },
      });
      // [argus patch #110] The hook refetches the agent, so the new commit
      // shows; the store's own answer says where the save landed.
      toast.success(
        (saved?.source ?? agent.source)?.kind === "github"
          ? t.agents.settingsSavedToGithub
          : t.agents.settingsSaved,
      );
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  // [argus patch #110] The soul editor; full screen shows only it, the way
  // the project document dialog does (patch #108).
  const soulSection = (
    <div
      className={cn(
        "space-y-1.5",
        soulExpanded && "flex min-h-0 flex-col gap-1.5 space-y-0",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{t.agents.settingsSoul}</span>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          aria-label={
            soulExpanded
              ? t.agents.settingsSoulExitFullScreen
              : t.agents.settingsSoulOpenFullScreen
          }
          title={
            soulExpanded
              ? t.agents.settingsSoulExitFullScreen
              : t.agents.settingsSoulOpenFullScreen
          }
          onClick={() => setSoulExpanded((value) => !value)}
        >
          {soulExpanded ? (
            <Minimize2 className="size-4" />
          ) : (
            <Maximize2 className="size-4" />
          )}
        </Button>
      </div>
      <p className="text-muted-foreground text-xs">
        {t.agents.settingsSoulHint}
      </p>
      <div
        className={cn(
          "overflow-hidden rounded-md border",
          soulExpanded ? "min-h-0 flex-1" : "h-80",
        )}
        data-testid="agent-soul-editor"
      >
        <CodeEditor
          className="size-full resize-none rounded-none border-none"
          value={soul}
          language="markdown"
          disabled={updateAgent.isPending}
          onChange={setSoul}
          onSave={() => void handleSave()}
        />
      </div>
    </div>
  );

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setSoulExpanded(false);
        onOpenChange(next);
      }}
    >
      <DialogContent
        className={cn(
          "grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden",
          // [argus patch #110] Room for the soul editor (upstream's 32rem is
          // too narrow for it); full screen like #108.
          soulExpanded
            ? "h-[100dvh] max-h-[100dvh] w-screen max-w-none rounded-none sm:max-w-none"
            : "max-h-[calc(100dvh-2rem)] sm:max-w-2xl lg:max-w-4xl",
        )}
        data-testid="agent-settings-dialog"
        data-expanded={soulExpanded ? "true" : "false"}
      >
        <DialogHeader className="pr-6">
          <DialogTitle>{t.agents.settingsTitle}</DialogTitle>
          <DialogDescription>{t.agents.settingsDescription}</DialogDescription>
          {/* [argus patch #110] Where this agent's definition lives. */}
          {githubSource && (
            <a
              href={githubSource.url}
              target="_blank"
              rel="noreferrer"
              className="text-primary inline-flex w-fit items-center gap-1.5 text-xs hover:underline"
            >
              <ExternalLinkIcon className="size-3.5" />
              {t.agents.openOnGithub}
              {githubSource.commit && (
                <code className="text-muted-foreground font-mono">
                  {githubSource.commit.slice(0, 7)}
                </code>
              )}
            </a>
          )}
        </DialogHeader>

        {/* [argus patch #110] Full screen shows only the soul editor; the
            other fields stay mounted (hidden) so nothing typed is lost. */}
        {soulExpanded && (
          <div className="flex min-h-0 min-w-0 flex-col px-1 py-1">
            {soulSection}
          </div>
        )}
        <div
          hidden={soulExpanded}
          className="min-h-0 min-w-0 space-y-4 overflow-y-auto overscroll-contain px-1 py-1"
        >
          <AgentCapabilitySelection
            plugins={plugins}
            skills={skills}
            toolGroups={toolGroups}
            onPluginsChange={setPlugins}
            onSkillsChange={setSkills}
            onToolGroupsChange={setToolGroups}
          />
          {(scopeSelectionEnabled || agent.knowledge_scope) && (
            <div className="space-y-1.5 rounded-md border p-3">
              <p className="text-sm font-medium">
                {t.agents.settingsKnowledge}
              </p>
              <p className="text-muted-foreground text-xs">
                {t.agents.settingsKnowledgeHint}
              </p>
              <KnowledgeScopeSelector
                agentName={agent.name}
                selection={knowledgeSelection}
                description={t.agents.settingsKnowledgeHint}
                showLabel
                disabled={updateAgent.isPending}
                unavailableReason={
                  !scopeSelectionEnabled
                    ? t.knowledge.scope.loadFailed
                    : !knowledgeAllowed
                      ? t.knowledge.scope.agentUnavailable
                      : knowledgeAccessNeedsSave
                        ? t.agents.settingsKnowledgeSaveToolAccess
                        : undefined
                }
                onChange={(selection) => {
                  setKnowledgeSelection(selection);
                  setKnowledgeChanged(true);
                }}
              />
              {scopeSelectionEnabled && knowledgeAccessNeedsSave && (
                <p className="text-muted-foreground text-xs">
                  {t.agents.settingsKnowledgeSaveToolAccess}
                </p>
              )}
              {knowledgeSelection.mode === "selected" && (
                <p className="text-muted-foreground text-xs [overflow-wrap:anywhere]">
                  {knowledgeSelection.datasets
                    .map((dataset) => dataset.name)
                    .join(", ")}
                </p>
              )}
              {knowledgeSelection.mode !== "all" && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={updateAgent.isPending}
                  onClick={() => {
                    setKnowledgeSelection({ mode: "all" });
                    setKnowledgeChanged(true);
                  }}
                >
                  {t.agents.settingsKnowledgeReset}
                </Button>
              )}
            </div>
          )}
          <div className="space-y-1.5">
            <label htmlFor="agent-display-name" className="text-sm font-medium">
              {t.agents.settingsDisplayName}
            </label>
            <Input
              id="agent-display-name"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder={agent.name}
              aria-describedby="agent-display-name-hint"
            />
            <p
              id="agent-display-name-hint"
              className="text-muted-foreground text-xs"
            >
              {t.agents.settingsDisplayNameHint} ({agent.name}){" · "}
              {[...displayName.trim()].length}/100
            </p>
          </div>
          {/* [argus patch #110] Description */}
          <div className="space-y-1.5">
            <label htmlFor="agent-description" className="text-sm font-medium">
              {t.agents.settingsAgentDescription}
            </label>
            <Textarea
              id="agent-description"
              value={description}
              rows={2}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t.agents.settingsAgentDescriptionPlaceholder}
            />
          </div>
          {!soulExpanded && soulSection}
          {/* Default model */}
          <div className="space-y-1.5">
            <span className="text-sm font-medium">
              {t.agents.settingsModel}
            </span>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger
                className="w-full"
                aria-label={t.agents.settingsModel}
                aria-describedby={
                  modelSwitchUnresolved ? modelStatusId : undefined
                }
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_MODEL_VALUE}>
                  {t.agents.settingsModelDefault}
                </SelectItem>
                {initialSelections.model !== DEFAULT_MODEL_VALUE &&
                  !models.some((m) => m.name === initialSelections.model) && (
                    <SelectItem value={initialSelections.model}>
                      {initialSelections.model}
                    </SelectItem>
                  )}
                {models.map((m) => (
                  <SelectItem key={m.name} value={m.name}>
                    {m.display_name || m.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {modelSwitchUnresolved && (
              <div className="space-y-1.5">
                <p
                  id={modelStatusId}
                  role="status"
                  className="text-muted-foreground text-xs"
                >
                  {t.agents.settingsModelSwitchNeedsCatalog}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={modelsFetching}
                  onClick={() => void refetchModels()}
                >
                  {modelsFetching
                    ? t.workspace.modelLoadRetrying
                    : t.workspace.modelLoadRetry}
                </Button>
              </div>
            )}
          </div>

          {/* Temperature */}
          <div className="space-y-1.5">
            <span className="text-sm font-medium">
              {t.agents.settingsTemperature}
            </span>
            <Input
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={temperature}
              placeholder={t.agents.settingsInherit}
              onChange={(e) => setTemperature(e.target.value)}
            />
            <p className="text-muted-foreground text-xs">
              {t.agents.settingsTemperatureHint}
            </p>
          </div>

          {/* Max output tokens */}
          <div className="space-y-1.5">
            <span className="text-sm font-medium">
              {t.agents.settingsMaxTokens}
            </span>
            <Input
              type="number"
              min={1}
              max={MAX_AGENT_OUTPUT_TOKENS}
              step={1}
              value={maxTokens}
              placeholder={t.agents.settingsMaxTokensPlaceholder}
              onChange={(e) => setMaxTokens(e.target.value)}
            />
          </div>

          {/* Keep incompatible saved values visible and explicitly clearable. */}
          {(supportsThinking || thinkingUnsupported) && (
            <div className="space-y-1.5">
              <span className="text-sm font-medium">
                {t.agents.settingsThinking}
              </span>
              <Select
                value={effectiveThinking}
                onValueChange={(value) => setThinking(value as typeof thinking)}
              >
                <SelectTrigger
                  className="w-full"
                  aria-label={t.agents.settingsThinking}
                  aria-describedby={
                    thinkingUnsupported && !normalizeReasoning
                      ? thinkingHintId
                      : undefined
                  }
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={INHERIT_VALUE}>
                    {t.agents.settingsInherit}
                  </SelectItem>
                  {supportsThinking && (
                    <SelectItem value="on">
                      {t.agents.settingsThinkingOn}
                    </SelectItem>
                  )}
                  {supportsThinking && !thinkingRequired && (
                    <SelectItem value="off">
                      {t.agents.settingsThinkingOff}
                    </SelectItem>
                  )}
                  {thinkingUnsupported && (
                    <SelectItem value={thinking} disabled>
                      {thinking === "off"
                        ? t.agents.settingsThinkingOff
                        : t.agents.settingsThinkingOn}{" "}
                      ({t.agents.settingsReasoningUnsupported})
                    </SelectItem>
                  )}
                </SelectContent>
              </Select>
              {thinkingUnsupported && !normalizeReasoning && (
                <p
                  id={thinkingHintId}
                  role="alert"
                  className="text-muted-foreground text-xs"
                >
                  {thinkingRequired
                    ? t.agents.settingsThinkingRequiredHint
                    : t.agents.settingsReasoningUnsupportedHint}
                </p>
              )}
            </div>
          )}

          {(supportsReasoningEffort || effortUnsupported) && (
            <div className="space-y-1.5">
              <span className="text-sm font-medium">
                {t.agents.settingsReasoningEffort}
              </span>
              <Select
                value={effectiveEffort}
                onValueChange={setReasoningEffort}
              >
                <SelectTrigger
                  className="w-full"
                  aria-label={t.agents.settingsReasoningEffort}
                  aria-describedby={
                    effortUnsupported && !normalizeReasoning
                      ? effortHintId
                      : undefined
                  }
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={INHERIT_VALUE}>
                    {t.agents.settingsInherit}
                  </SelectItem>
                  {effortUnsupported && (
                    <SelectItem value={reasoningEffort} disabled>
                      {reasoningEffort} ({t.agents.settingsReasoningUnsupported}
                      )
                    </SelectItem>
                  )}
                  {reasoningEffortOptions.map((effort) => (
                    <SelectItem key={effort} value={effort}>
                      {effort}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {effortUnsupported && !normalizeReasoning && (
                <p id={effortHintId} className="text-muted-foreground text-xs">
                  {t.agents.settingsReasoningUnsupportedHint}
                </p>
              )}
            </div>
          )}

          <div className="space-y-2 border-t pt-4">
            <div>
              <p className="text-sm font-medium">
                {t.settings.subagents.bindingTitle}
              </p>
              <p className="text-muted-foreground text-xs">
                {t.settings.subagents.bindingDescription}
              </p>
            </div>
            <Select
              value={subagentAccess}
              onValueChange={(value) =>
                setSubagentAccess(value as SubagentAccessSelection)
              }
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">
                  {t.settings.subagents.allAllowed}
                </SelectItem>
                <SelectItem value="none">
                  {t.settings.subagents.noneAllowed}
                </SelectItem>
                <SelectItem value="selected">
                  {t.settings.subagents.selectedAllowed}
                </SelectItem>
              </SelectContent>
            </Select>
            {subagentAccess === "selected" && (
              <div className="space-y-3 rounded-md border p-3">
                {selectableSubagents.map((item) => (
                  <div key={item.name} className="min-w-0 space-y-1">
                    <label className="flex items-start gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="mt-0.5 size-4 shrink-0"
                        checked={selectedSubagents.includes(item.name)}
                        onChange={(event) =>
                          setSelectedSubagents((current) =>
                            event.target.checked
                              ? [...current, item.name]
                              : current.filter((name) => name !== item.name),
                          )
                        }
                      />
                      <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
                        {item.display_name ?? item.name}
                      </span>
                    </label>
                    {item.description && (
                      <details
                        className="group text-muted-foreground ml-6 text-xs"
                        onToggle={(event) => {
                          if (event.currentTarget.open) {
                            event.currentTarget
                              .querySelector("p")
                              ?.scrollIntoView({ block: "nearest" });
                          }
                        }}
                      >
                        <summary
                          className="focus-visible:ring-ring flex cursor-pointer list-none items-start gap-1 rounded-sm focus-visible:ring-2 [&::-webkit-details-marker]:hidden"
                          aria-label={`${item.display_name ?? item.name}: ${t.settings.subagents.descriptionLabel}`}
                          aria-describedby={`${subagentDescriptionId}-${item.name}`}
                        >
                          <ChevronRightIcon className="size-3 shrink-0 transition-transform group-open:rotate-90" />
                          <span
                            id={`${subagentDescriptionId}-${item.name}`}
                            className="line-clamp-2 [overflow-wrap:anywhere] group-open:hidden"
                          >
                            {item.description}
                          </span>
                          <span className="hidden group-open:inline">
                            {t.settings.subagents.descriptionLabel}
                          </span>
                        </summary>
                        <p className="mt-1 [overflow-wrap:anywhere] whitespace-pre-wrap">
                          {item.description}
                        </p>
                      </details>
                    )}
                  </div>
                ))}
                {missingSubagents.map((name) => (
                  <label
                    key={name}
                    className="text-muted-foreground flex items-start gap-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5 size-4 shrink-0"
                      checked
                      onChange={() =>
                        setSelectedSubagents((current) =>
                          current.filter((item) => item !== name),
                        )
                      }
                    />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="font-medium">{name}</span>
                      <span className="block text-xs">
                        {t.settings.subagents.missing}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={updateAgent.isPending}
          >
            {t.common.cancel}
          </Button>
          <Button
            onClick={handleSave}
            disabled={updateAgent.isPending || modelSwitchUnresolved}
          >
            {updateAgent.isPending ? t.common.loading : t.common.save}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
