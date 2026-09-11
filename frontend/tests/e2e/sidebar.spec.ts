import { expect, test } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

test.describe("Sidebar navigation", () => {
  test("sidebar contains Chats, Agents, and the external Akropolis links", async ({
    page,
  }) => {
    mockLangGraphAPI(page);

    await page.goto("/workspace/chats/new");

    const sidebar = page.locator("[data-sidebar='sidebar']");
    await expect(sidebar.locator("a[href='/workspace/chats']")).toBeVisible({
      timeout: 15_000,
    });

    for (const { label, href } of [
      {
        label: "Chronos",
        href: "https://chronos.acro.surfly.com/jobs",
      },
      {
        label: "Handbook",
        href: "https://apps-nicholas.acro.surfly.com/akropolis-handbook/",
      },
      {
        label: "Feature Requests",
        href: "https://apps-nicholas.acro.surfly.com/acropolis-feature-requests/",
      },
    ]) {
      const link = sidebar.getByRole("link", { name: label, exact: true });
      await expect(link).toHaveAttribute("href", href);
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
      await expect(link.locator("svg")).toHaveClass(/text-muted-foreground/);
    }

    const agentsLink = sidebar.getByRole("link", {
      name: "Agents",
      exact: true,
    });
    await expect(agentsLink).toBeVisible();
    await expect(agentsLink).toHaveAttribute("href", "/workspace/agents");
    await expect(
      sidebar.locator("a[href='/workspace/scheduled-tasks']"),
    ).toHaveCount(0);
  });

  test("mobile welcome layout stays within viewport and opens sidebar", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 664 });
    mockLangGraphAPI(page);

    await page.goto("/workspace/chats/new");
    await page.evaluate(() => {
      document.cookie = "locale=zh-CN; path=/; SameSite=Lax";
    });
    await page.reload();

    const viewportWidth = page.viewportSize()?.width ?? 390;
    const expectInsideViewport = async (
      locator: ReturnType<typeof page.locator>,
    ) => {
      await expect(locator).toBeVisible({ timeout: 15_000 });
      const box = await locator.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(-1);
      expect(box!.x + box!.width).toBeLessThanOrEqual(viewportWidth + 1);
    };

    await expectInsideViewport(page.getByText(/欢迎使用 🦌 DeerFlow/).first());
    await expectInsideViewport(page.getByRole("textbox").first());
    await expectInsideViewport(page.locator("[data-slot='suggestions-list']"));

    const mobileSidebarTrigger = page
      .locator("[data-sidebar='trigger']:visible")
      .first();
    await expect(mobileSidebarTrigger).toBeVisible();
    const triggerBox = await mobileSidebarTrigger.boundingBox();
    expect(triggerBox).not.toBeNull();
    const triggerReceivesPointerEvents = await page.evaluate(
      ({ x, y }) => {
        const trigger = document.elementFromPoint(x, y);
        return trigger?.closest("[data-sidebar='trigger']") !== null;
      },
      {
        x: triggerBox!.x + triggerBox!.width / 2,
        y: triggerBox!.y + triggerBox!.height / 2,
      },
    );
    expect(triggerReceivesPointerEvents).toBe(true);
    await page.mouse.click(
      triggerBox!.x + triggerBox!.width / 2,
      triggerBox!.y + triggerBox!.height / 2,
    );

    const mobileSidebar = page.locator(
      "[data-mobile='true'][data-sidebar='sidebar']",
    );
    await expect(mobileSidebar).toBeVisible();
    await expect(
      mobileSidebar.locator("a[href='/workspace/chats']"),
    ).toBeVisible();
    await expect(
      mobileSidebar.getByRole("link", { name: "Chronos" }),
    ).toBeVisible();
    await expect(
      mobileSidebar.locator("a[href='/workspace/agents']"),
    ).toBeVisible();
  });
});
