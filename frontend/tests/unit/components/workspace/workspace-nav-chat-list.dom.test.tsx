import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import { SidebarProvider } from "@/components/ui/sidebar";
import { WorkspaceNavChatList } from "@/components/workspace/workspace-nav-chat-list";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";

rs.mock("next/navigation", () => ({
  useRouter: () => ({ push: rs.fn(), replace: rs.fn(), refresh: rs.fn() }),
  usePathname: () => "/workspace/chats",
}));

rs.mock("@/core/agents", () => ({
  useAgentsApiEnabled: () => ({ enabled: false, isLoading: false }),
}));

// [argus patch #95] Mutable so each test picks the flag /api/features reports.
const capabilityCenter = rs.hoisted(() => ({ enabled: true }));
rs.mock("@/core/features/hooks", () => ({
  useCapabilityCenterEnabled: () => ({
    enabled: capabilityCenter.enabled,
    isLoading: false,
  }),
}));

function renderNav() {
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <SidebarProvider>
        <WorkspaceNavChatList />
      </SidebarProvider>
    </I18nContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  capabilityCenter.enabled = true;
});

describe("WorkspaceNavChatList (DOM)", () => {
  it("links to the Agora knowledge page in a new tab, after Chronos", () => {
    renderNav();
    const agora = screen.getByRole("link", { name: /Agora/ });
    expect(agora.getAttribute("href")).toBe(
      "https://agora.acro.surfly.com/knowledge",
    );
    expect(agora.getAttribute("target")).toBe("_blank");
    expect(agora.getAttribute("rel")).toContain("noopener");

    const labels = screen
      .getAllByRole("link")
      .map((link) => link.textContent?.trim() ?? "");
    expect(labels.indexOf("Agora")).toBe(labels.indexOf("Chronos") + 1);
  });

  it("shows the Capability Center when the feature flag is on", () => {
    renderNav();
    expect(
      screen.getByRole("link", { name: enUS.capabilities.title }),
    ).toBeTruthy();
  });

  it("hides the Capability Center when the feature flag is off", () => {
    capabilityCenter.enabled = false;
    renderNav();
    expect(
      screen.queryByRole("link", { name: enUS.capabilities.title }),
    ).toBeNull();
  });
});
