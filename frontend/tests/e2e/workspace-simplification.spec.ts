import { expect, test } from "@playwright/test";

import { mockLangGraphAPI, MOCK_THREAD_ID } from "./utils/mock-api";

test("chat header keeps context and debug while hiding token and browser controls", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    threads: [
      {
        thread_id: MOCK_THREAD_ID,
        title: "Simplified workspace",
        messages: [
          { type: "human", id: "human-1", content: "Hello" },
          {
            type: "ai",
            id: "ai-1",
            content: "Done",
            usage_metadata: {
              input_tokens: 100,
              output_tokens: 25,
              total_tokens: 125,
            },
          },
        ],
      },
    ],
    features: { browserControlEnabled: true },
  });
  await page.route("**/api/models", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        models: [],
        token_usage: { enabled: true },
      }),
    }),
  );
  await page.route(`**/api/threads/${MOCK_THREAD_ID}/token-usage`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        thread_id: MOCK_THREAD_ID,
        input_tokens: 100,
        output_tokens: 25,
        total_tokens: 125,
        context_usage: {
          current_tokens: 25_000,
          max_context_tokens: 100_000,
          percentage: 25,
        },
      }),
    }),
  );

  await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);

  const header = page.locator("header");
  await expect(header.getByRole("status", { name: /25% full/i })).toBeVisible({
    timeout: 15_000,
  });
  await expect(header.getByRole("button", { name: "Tokens" })).toHaveCount(0);
  await expect(page.getByTestId("browser-trigger")).toHaveCount(0);
  await expect(
    header.getByRole("button", { name: "Live Desktop & Browser" }),
  ).toBeVisible();

  const exportButton = header.getByRole("button", { name: "Export" });
  await expect(exportButton).toBeVisible();
  await expect(exportButton).toHaveText("");
  await exportButton.click();
  await expect(page.getByText("Export as Markdown")).toBeVisible();

  await expect(page.getByText(/Input:\s*100/)).toHaveCount(0);
  await expect(page.getByText(/Output:\s*25/)).toHaveCount(0);
});
