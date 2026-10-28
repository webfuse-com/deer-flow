import { safeLocalStorage } from "@/core/settings/local";

import { threadOriginOf, type ThreadOrigin } from "./origin";
import type { AgentThread } from "./types";
import {
  THREAD_PINNED_METADATA_KEY,
  THREAD_PROJECT_METADATA_KEY,
} from "./utils";

/**
 * [argus patch #169] The Chats page filter chips. Origin chips (Mine,
 * Scheduled, Telegram) and "In a project" are decided in the browser from the
 * metadata every listed thread carries, inside the infinite-list page loop so
 * a filtered page still fills. Pinned, Builders and Artifacts are boolean
 * metadata flags, so the search API filters them on the server (exact match).
 */
export const THREAD_LIST_FILTERS = [
  "all",
  "mine",
  "scheduled",
  "channels",
  "project",
  "pinned",
  "builders",
  "artifacts",
] as const;

export type ThreadListFilter = (typeof THREAD_LIST_FILTERS)[number];

/**
 * Set by the Argus DeerFlow extension when a run ends: the run called an
 * external builder (`build_delegate`, `code_delegate`), or the thread's
 * outputs directory holds files.
 */
export const THREAD_BUILDER_METADATA_KEY = "argus_builder";
export const THREAD_ARTIFACT_METADATA_KEY = "argus_artifact";

/** Argus's nightly Aeacus jobs tag their threads `{"aeacus": {...}}`. */
const AEACUS_METADATA_KEY = "aeacus";

const SERVER_FLAG_KEYS: Partial<Record<ThreadListFilter, string>> = {
  pinned: THREAD_PINNED_METADATA_KEY,
  builders: THREAD_BUILDER_METADATA_KEY,
  artifacts: THREAD_ARTIFACT_METADATA_KEY,
};

export function isThreadListFilter(value: unknown): value is ThreadListFilter {
  return (
    typeof value === "string" &&
    (THREAD_LIST_FILTERS as readonly string[]).includes(value)
  );
}

/** The search API's exact-match metadata filter for a chip, or `null`. */
export function serverMetadataOfThreadListFilter(
  filter: ThreadListFilter,
): Record<string, true> | null {
  const key = SERVER_FLAG_KEYS[filter];
  return key ? { [key]: true } : null;
}

function isScheduledThread(
  metadata: Record<string, unknown>,
  origin: ThreadOrigin | null,
): boolean {
  if (origin?.kind === "schedule" || AEACUS_METADATA_KEY in metadata) {
    return true;
  }
  // Schedule chats created before the "sched:<id>" topic carried the
  // schedule in the chat id instead ("<chat>:sched:<id>").
  if (origin?.kind !== "im_channel") {
    return false;
  }
  const chatId: unknown = Reflect.get(
    (metadata.channel_source as object | null | undefined) ?? {},
    "chat_id",
  );
  return typeof chatId === "string" && chatId.includes(":sched:");
}

export function threadMatchesListFilter(
  thread: Pick<AgentThread, "metadata">,
  filter: ThreadListFilter,
): boolean {
  const metadata = (thread.metadata ?? {}) as Record<string, unknown>;
  const flagKey = SERVER_FLAG_KEYS[filter];
  if (flagKey) {
    return metadata[flagKey] === true;
  }
  if (filter === "all") {
    return true;
  }
  if (filter === "project") {
    const projectId = metadata[THREAD_PROJECT_METADATA_KEY];
    return typeof projectId === "string" && projectId.length > 0;
  }
  const origin = threadOriginOf(thread);
  const scheduled = isScheduledThread(metadata, origin);
  if (filter === "scheduled") {
    return scheduled;
  }
  if (filter === "channels") {
    return origin?.kind === "im_channel" && !scheduled;
  }
  return origin === null && !scheduled;
}

const SAVED_FILTER_KEY = "deerflow.chats-filter";
const savedFilterListeners = new Set<() => void>();
// Keeps the choice for this page load when storage writes fail.
let memoryFilter: ThreadListFilter | null = null;

/** The chip this browser last chose on the Chats page ("all" by default). */
export function readSavedThreadListFilter(): ThreadListFilter {
  if (memoryFilter) {
    return memoryFilter;
  }
  const saved = safeLocalStorage.getItem(SAVED_FILTER_KEY);
  return isThreadListFilter(saved) ? saved : "all";
}

export function saveThreadListFilter(filter: ThreadListFilter) {
  memoryFilter = filter;
  safeLocalStorage.setItem(SAVED_FILTER_KEY, filter);
  for (const listener of savedFilterListeners) {
    listener();
  }
}

export function subscribeSavedThreadListFilter(listener: () => void) {
  savedFilterListeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === SAVED_FILTER_KEY) {
      memoryFilter = null;
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    savedFilterListeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}
