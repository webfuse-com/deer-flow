import { describe, expect, test } from "@rstest/core";

import {
  isThreadListFilter,
  serverMetadataOfThreadListFilter,
  THREAD_LIST_FILTERS,
  threadMatchesListFilter,
  type ThreadListFilter,
} from "@/core/threads/list-filter";
import type { AgentThread } from "@/core/threads/types";

// [argus patch #169] Which Chats page chip shows which thread. The metadata
// shapes are the ones atlas-nicholas threads carried on 2026-10-27.
function thread(
  metadata: Record<string, unknown>,
): Pick<AgentThread, "metadata"> {
  return { metadata } as Pick<AgentThread, "metadata">;
}

const THREADS: Record<string, Pick<AgentThread, "metadata">> = {
  own: thread({ agent_name: "atlas" }),
  ownTelegram: thread({
    channel_source: {
      type: "im_channel",
      provider: "telegram",
      chat_id: "8726302666",
    },
  }),
  telegramSchedule: thread({
    channel_source: {
      type: "im_channel",
      provider: "telegram",
      chat_id: "8726302666",
      topic_id: "sched:meeting-prep",
    },
  }),
  legacyTelegramSchedule: thread({
    channel_source: {
      type: "im_channel",
      provider: "telegram",
      chat_id: "8726302666:sched:weekly-metrics",
    },
  }),
  webSchedule: thread({
    deerflow_origin: { kind: "schedule" },
    argus_schedule_id: "daily-review",
  }),
  aeacus: thread({
    aeacus: { job_id: "aeacus-20261001-it-5675", key: "it-5675" },
  }),
  extension: thread({
    deerflow_origin: { kind: "extension", namespace: "bookmarks" },
  }),
  project: thread({ deerflow_project_id: "proj-1" }),
  pinned: thread({ deerflow_pinned: true }),
  builder: thread({ argus_builder: true }),
  artifact: thread({ argus_artifact: true }),
};

function matching(filter: ThreadListFilter): string[] {
  return Object.entries(THREADS)
    .filter(([, value]) => threadMatchesListFilter(value, filter))
    .map(([name]) => name);
}

describe("threadMatchesListFilter", () => {
  test("All shows everything", () => {
    expect(matching("all")).toEqual(Object.keys(THREADS));
  });

  test("Mine shows chats nobody else started", () => {
    expect(matching("mine")).toEqual([
      "own",
      "project",
      "pinned",
      "builder",
      "artifact",
    ]);
  });

  test("Scheduled covers web and Telegram schedules and Aeacus nights", () => {
    expect(matching("scheduled")).toEqual([
      "telegramSchedule",
      "legacyTelegramSchedule",
      "webSchedule",
      "aeacus",
    ]);
  });

  test("Telegram shows channel chats that no schedule started", () => {
    expect(matching("channels")).toEqual(["ownTelegram"]);
  });

  test("flags and projects match only their own threads", () => {
    expect(matching("project")).toEqual(["project"]);
    expect(matching("pinned")).toEqual(["pinned"]);
    expect(matching("builders")).toEqual(["builder"]);
    expect(matching("artifacts")).toEqual(["artifact"]);
  });

  test("a thread without metadata counts as the user's own", () => {
    const bare = { metadata: undefined } as unknown as Pick<
      AgentThread,
      "metadata"
    >;
    expect(threadMatchesListFilter(bare, "mine")).toBe(true);
    expect(threadMatchesListFilter(bare, "pinned")).toBe(false);
  });
});

describe("serverMetadataOfThreadListFilter", () => {
  test("only boolean flags go to the server", () => {
    expect(
      Object.fromEntries(
        THREAD_LIST_FILTERS.map((filter) => [
          filter,
          serverMetadataOfThreadListFilter(filter),
        ]),
      ),
    ).toEqual({
      all: null,
      mine: null,
      scheduled: null,
      channels: null,
      project: null,
      pinned: { deerflow_pinned: true },
      builders: { argus_builder: true },
      artifacts: { argus_artifact: true },
    });
  });
});

describe("isThreadListFilter", () => {
  test("accepts chip values only", () => {
    expect(isThreadListFilter("mine")).toBe(true);
    expect(isThreadListFilter("telegram")).toBe(false);
    expect(isThreadListFilter(null)).toBe(false);
  });
});
