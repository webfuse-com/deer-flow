import { expect, test } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

// [argus patch #169] Chats page filter chips.
const THREADS = [
  {
    thread_id: "00000000-0000-0000-0000-000000000a01",
    title: "Pricing page review",
    updated_at: "2026-10-27T10:00:00Z",
    metadata: { argus_builder: true },
  },
  {
    thread_id: "00000000-0000-0000-0000-000000000a02",
    title: "Meeting prep 10-27",
    updated_at: "2026-10-27T09:00:00Z",
    metadata: {
      channel_source: {
        type: "im_channel",
        provider: "telegram",
        chat_id: "100",
        topic_id: "sched:meeting-prep",
      },
    },
  },
  {
    thread_id: "00000000-0000-0000-0000-000000000a03",
    title: "Aeacus it-5675",
    updated_at: "2026-10-27T08:00:00Z",
    metadata: { aeacus: { key: "it-5675" } },
  },
  {
    thread_id: "00000000-0000-0000-0000-000000000a04",
    title: "Board deck notes",
    updated_at: "2026-10-27T07:00:00Z",
    metadata: { deerflow_pinned: true },
  },
];

test("chips filter the chats list, stay in the address and are remembered", async ({
  page,
}) => {
  mockLangGraphAPI(page, { threads: THREADS });
  await page.goto("/workspace/chats", { waitUntil: "domcontentloaded" });

  const list = page.locator("main");
  const chips = page.getByRole("group", { name: "Show" });
  const titles = THREADS.map((thread) => thread.title);
  for (const title of titles) {
    await expect(list.getByText(title, { exact: true })).toBeVisible();
  }

  await chips.getByRole("radio", { name: "Mine", exact: true }).click();
  await expect(page).toHaveURL(/\?show=mine$/);
  await expect(
    list.getByText("Pricing page review", { exact: true }),
  ).toBeVisible();
  await expect(
    list.getByText("Board deck notes", { exact: true }),
  ).toBeVisible();
  await expect(
    list.getByText("Meeting prep 10-27", { exact: true }),
  ).toHaveCount(0);
  await expect(list.getByText("Aeacus it-5675", { exact: true })).toHaveCount(
    0,
  );

  await chips.getByRole("radio", { name: "Scheduled", exact: true }).click();
  await expect(
    list.getByText("Meeting prep 10-27", { exact: true }),
  ).toBeVisible();
  await expect(list.getByText("Aeacus it-5675", { exact: true })).toBeVisible();
  await expect(
    list.getByText("Pricing page review", { exact: true }),
  ).toHaveCount(0);

  // Pinned and Builders are filtered by the search API.
  const searchBody = page.waitForRequest(
    (request) =>
      request.url().endsWith("/threads/search") &&
      JSON.stringify(request.postDataJSON()?.metadata) ===
        JSON.stringify({ argus_builder: true }),
  );
  await chips.getByRole("radio", { name: "Builders", exact: true }).click();
  await searchBody;
  await expect(
    list.getByText("Pricing page review", { exact: true }),
  ).toBeVisible();
  await expect(list.getByText("Board deck notes", { exact: true })).toHaveCount(
    0,
  );

  await chips.getByRole("radio", { name: "Telegram", exact: true }).click();
  await expect(
    page.getByText("No chats match this filter", { exact: true }),
  ).toBeVisible();

  // The last choice comes back without the address carrying it.
  await chips.getByRole("radio", { name: "Pinned", exact: true }).click();
  await page.goto("/workspace/chats", { waitUntil: "domcontentloaded" });
  await expect(
    chips.getByRole("radio", { name: "Pinned", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(
    list.getByText("Board deck notes", { exact: true }),
  ).toBeVisible();
  await expect(
    list.getByText("Pricing page review", { exact: true }),
  ).toHaveCount(0);

  await chips.getByRole("radio", { name: "All", exact: true }).click();
  await expect(page).toHaveURL(/\/workspace\/chats$/);
  for (const title of titles) {
    await expect(list.getByText(title, { exact: true })).toBeVisible();
  }
});
