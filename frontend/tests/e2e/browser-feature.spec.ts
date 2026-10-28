import { expect, test } from "@playwright/test";

import { MOCK_THREAD_ID, mockLangGraphAPI } from "./utils/mock-api";

// [argus patch #168] The trigger reopens the panel on the agent's own
// browser frame, so it needs one in the thread.
const browsedMessages = [
  { type: "human", id: "human-browse", content: "Open example.com" },
  {
    type: "ai",
    id: "ai-browse",
    content: "",
    tool_calls: [
      {
        id: "call-browse",
        name: "browser_navigate",
        args: { url: "https://example.com" },
      },
    ],
  },
  {
    type: "tool",
    id: "tool-browse",
    tool_call_id: "call-browse",
    name: "browser_navigate",
    content: "Opened https://example.com",
    additional_kwargs: {
      browser_view: {
        screenshot: "/mnt/user-data/outputs/browser.png",
        url: "https://example.com",
        title: "Example Domain",
      },
    },
  },
  { type: "ai", id: "ai-browsed", content: "Opened it." },
];

test.describe("Browser feature flag", () => {
  test("shows browser trigger once the agent has browsed", async ({ page }) => {
    mockLangGraphAPI(page, {
      threads: [
        {
          thread_id: MOCK_THREAD_ID,
          title: "Browser Enabled",
          messages: browsedMessages,
        },
      ],
    });

    await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);

    await expect(page.getByTestId("browser-trigger")).toBeVisible({
      timeout: 15_000,
    });
  });

  test("hides browser trigger until the agent has browsed", async ({
    page,
  }) => {
    mockLangGraphAPI(page, {
      threads: [
        {
          thread_id: MOCK_THREAD_ID,
          title: "Browser Enabled",
          messages: [{ type: "ai", id: "ai-1", content: "Hello" }],
        },
      ],
    });

    await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);
    await expect(page.getByText("Hello")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("browser-trigger")).toHaveCount(0);
  });

  test("hides browser trigger when browser_control is disabled", async ({
    page,
  }) => {
    mockLangGraphAPI(page, {
      threads: [{ thread_id: MOCK_THREAD_ID, title: "Browser Disabled" }],
      features: { browserControlEnabled: false },
    });

    const featuresResponse = page.waitForResponse((response) =>
      response.url().includes("/api/features"),
    );
    await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);
    const features = (await (await featuresResponse).json()) as {
      browser_control?: { enabled?: boolean };
    };
    expect(features.browser_control?.enabled).toBe(false);

    await expect(page.getByTestId("browser-trigger")).toHaveCount(0);
  });
});
