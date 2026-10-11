"use client";

import { CalendarClock, ExternalLink, Play } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { fill } from "@/components/workspace/scheduled-tasks/shared";
import { useScheduledTasksFeature } from "@/core/features";
import { useI18n } from "@/core/i18n/hooks";
import {
  usePauseScheduledTask,
  useResumeScheduledTask,
  useThreadScheduledTasks,
  useTriggerScheduledTask,
} from "@/core/scheduled-tasks/hooks";
import type { ThreadScheduledTask } from "@/core/scheduled-tasks/types";

const TASKS_PAGE = "/workspace/scheduled-tasks";

export type ThreadScheduledTasksTarget =
  | { kind: "run"; taskId: string }
  | { kind: "tasks"; count: number; href: string };

/**
 * Where the chat header button leads. A run conversation (the thread only
 * holds runs of a task) links to that task; a chat that created or hosts
 * tasks links to its one task, or to the list filtered to this chat.
 */
export function threadScheduledTasksTarget(
  threadId: string,
  tasks: readonly ThreadScheduledTask[],
): ThreadScheduledTasksTarget | null {
  if (tasks.length === 0) {
    return null;
  }
  const owned = tasks.filter((task) => task.thread_relation !== "run");
  if (owned.length === 0) {
    return { kind: "run", taskId: tasks[0]!.id };
  }
  const href =
    owned.length === 1
      ? `${TASKS_PAGE}?task_id=${encodeURIComponent(owned[0]!.id)}`
      : `${TASKS_PAGE}?thread_id=${encodeURIComponent(threadId)}`;
  return { kind: "tasks", count: owned.length, href };
}

/**
 * Chat header entry to the scheduled tasks of this chat. Hidden when the
 * feature is unavailable, while loading, on error, or when the chat has no
 * task.
 */
export function ThreadScheduledTasksButton({ threadId }: { threadId: string }) {
  const { t } = useI18n();
  const feature = useScheduledTasksFeature();
  const query = useThreadScheduledTasks(threadId, {
    enabled: feature.available,
  });
  if (!feature.available || query.isError || !query.data) {
    return null;
  }
  if (feature.external && query.data.length === 1) {
    return <SingleTaskMenu task={query.data[0]!} />;
  }
  const target = threadScheduledTasksTarget(threadId, query.data);
  if (!target) {
    return null;
  }
  if (target.kind === "run") {
    const label = t.scheduledTasks.header.runTask;
    return (
      <Button variant="outline" size="sm" asChild>
        <Link
          aria-label={label}
          href={`${TASKS_PAGE}?task_id=${encodeURIComponent(target.taskId)}`}
          data-testid="thread-scheduled-tasks-button"
        >
          <CalendarClock aria-hidden />
          <span className="hidden sm:inline">{label}</span>
        </Link>
      </Button>
    );
  }
  const countLabel =
    target.count === 1
      ? t.scheduledTasks.header.countLabelOne
      : fill(t.scheduledTasks.header.countLabel, { count: target.count });
  return (
    <Button variant="outline" size="sm" asChild>
      <Link
        aria-label={countLabel}
        title={countLabel}
        href={target.href}
        data-testid="thread-scheduled-tasks-button"
      >
        <CalendarClock aria-hidden />
        <span className="hidden sm:inline">{t.sidebar.scheduledTasks}</span>
        <span
          className="bg-primary text-primary-foreground grid size-4 place-items-center rounded-full text-[10px] font-semibold"
          aria-hidden
        >
          {target.count > 9 ? "9+" : target.count}
        </span>
      </Link>
    </Button>
  );
}

/**
 * [argus patch #116] The one schedule of this chat, switched and run from the
 * header: an "Active" check (pause / resume), Run once now, Open task.
 */
function SingleTaskMenu({ task }: { task: ThreadScheduledTask }) {
  const { t } = useI18n();
  const st = t.scheduledTasks;
  const pause = usePauseScheduledTask();
  const resume = useResumeScheduledTask();
  const trigger = useTriggerScheduledTask();
  const active = task.status === "enabled" || task.status === "running";
  const switchable = active || task.status === "paused";
  const busy = pause.isPending || resume.isPending;
  const title = task.title || st.header.runTask;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-label={`${st.header.menuLabel}: ${title}`}
          data-testid="thread-scheduled-tasks-button"
        >
          <CalendarClock
            aria-hidden
            className={active ? undefined : "opacity-50"}
          />
          <span className="hidden max-w-40 truncate sm:inline">{title}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel className="text-muted-foreground text-xs font-normal">
          {st.header.menuLabel}
        </DropdownMenuLabel>
        <DropdownMenuCheckboxItem
          checked={active}
          disabled={!switchable || busy}
          onCheckedChange={(checked) =>
            checked ? resume.mutate({ taskId: task.id }) : pause.mutate(task.id)
          }
        >
          {st.header.active}
        </DropdownMenuCheckboxItem>
        <DropdownMenuItem
          disabled={trigger.isPending}
          onSelect={() => trigger.mutate(task.id)}
        >
          <Play aria-hidden />
          {st.actions.runNow}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href={`${TASKS_PAGE}?task_id=${encodeURIComponent(task.id)}`}>
            <ExternalLink aria-hidden />
            {st.actions.openTask}
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
