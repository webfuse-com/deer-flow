import { expect, test } from "@playwright/test";

import { mockLangGraphAPI, MOCK_THREAD_ID } from "./utils/mock-api";

// Argus header controls (patch #38 debug sandbox, patch #88 Internalize) sit
// next to upstream's token, browser and export controls.
test("chat header shows the Argus controls next to upstream's", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    threads: [
      {
        thread_id: MOCK_THREAD_ID,
        title: "Argus header",
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

  // Argus patch #88: the Internalize control asks the Agora for the thread's
  // share state; answer "not shared" so the header renders the plain button.
  await page.route("**/api/shared-threads/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        thread_id: MOCK_THREAD_ID,
        stack: "atlas-test",
        shared: false,
        share: null,
        share_url: `https://agora.example/threads/shared/atlas-test/${MOCK_THREAD_ID}`,
        view_path: `/workspace/shared/atlas-test/${MOCK_THREAD_ID}`,
      }),
    }),
  );

  await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);

  const header = page.locator("header");
  const tokensButton = header.getByRole("button", { name: /Tokens/ });
  await expect(tokensButton).toBeVisible({ timeout: 15_000 });
  await expect(tokensButton).toContainText("25%");
  await expect(page.getByTestId("browser-trigger")).toBeVisible();
  await expect(
    header.getByRole("button", { name: "Live Desktop & Browser" }),
  ).toBeVisible();

  // Argus patch #88: the Internalize control sits in the same cluster. Check
  // it before opening the export menu, which hides everything else from the
  // accessibility tree while it is open.
  const internalizeButton = header.getByRole("button", {
    name: "Internalize",
  });
  await expect(internalizeButton).toBeVisible();
  await expect(internalizeButton).toHaveText("");

  const exportButton = header.getByRole("button", { name: "Export" });
  await expect(exportButton).toBeVisible();
  await exportButton.click();
  await expect(page.getByText("Export as Markdown")).toBeVisible();
});
