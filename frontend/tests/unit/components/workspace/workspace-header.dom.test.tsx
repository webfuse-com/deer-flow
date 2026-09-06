import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import { SidebarProvider } from "@/components/ui/sidebar";
import { WorkspaceHeader } from "@/components/workspace/workspace-header";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";

rs.mock("next/navigation", () => ({
  useRouter: () => ({ push: rs.fn(), replace: rs.fn(), refresh: rs.fn() }),
  usePathname: () => "/workspace/chats",
}));

rs.mock("@/env", () => ({
  env: { NEXT_PUBLIC_STATIC_WEBSITE_ONLY: "false" },
}));

function renderHeader(defaultOpen: boolean) {
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <SidebarProvider defaultOpen={defaultOpen}>
        <WorkspaceHeader />
      </SidebarProvider>
    </I18nContext.Provider>,
  );
}

afterEach(cleanup);

describe("WorkspaceHeader (DOM)", () => {
  it("shows a solid star as the collapsed brand mark, never the DF text", () => {
    renderHeader(false);
    const mark = screen.getByTestId("brand-mark");
    expect(mark.getAttribute("aria-label")).toBe("Atlas");
    const star = mark.querySelector("svg");
    expect(star).not.toBeNull();
    expect(star?.getAttribute("fill")).toBe("currentColor");
    expect(mark.textContent?.trim()).toBe("");
    expect(screen.queryByText("DF")).toBeNull();
  });

  it("keeps the DeerFlow word with the same star when expanded", () => {
    renderHeader(true);
    const mark = screen.getByTestId("brand-mark");
    expect(mark.textContent).toContain("DeerFlow");
    expect(mark.querySelector("svg")?.getAttribute("fill")).toBe(
      "currentColor",
    );
    expect(screen.queryByText("DF")).toBeNull();
  });
});
