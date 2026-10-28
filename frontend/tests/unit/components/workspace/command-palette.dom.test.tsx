import { afterEach, beforeAll, describe, expect, it, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { CommandPalette } from "@/components/workspace/command-palette";
import { setCommandPaletteOpen } from "@/components/workspace/command-palette-store";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";

// [argus patch #167]
const push = rs.hoisted(() => rs.fn());
rs.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: rs.fn(), refresh: rs.fn() }),
  usePathname: () => "/workspace/chats",
}));
rs.mock("@/components/workspace/settings", () => ({
  useSettingsDialog: () => ({ openSettings: rs.fn() }),
}));
rs.mock("@/core/projects", () => ({
  useProjects: () => ({
    data: [
      {
        id: "p1",
        name: "Semantic Selectors",
        presentation: { argus_slug: "semantic-selectors" },
      },
      { id: "p2", name: "Office Reminders", presentation: {} },
    ],
  }),
}));
rs.mock("@/core/extensions/hooks", () => ({
  useFrontendExtensions: () => ({
    data: [
      {
        namespace: "argus",
        module: "argus.v1",
        entry: null,
        title: "Argus",
        description: "",
        settings: { enabled: true },
        extension: {
          apiVersion: 1,
          module: "argus.v1",
          commandProviders: [
            {
              id: "apps",
              label: "Apps",
              position: "start",
              items: async () => [
                {
                  id: "app:atlas-nicholas/semantic-selectors",
                  label: "STR-2026-0814: Resilient Web Element Relocalization",
                  description: "semantic-selectors · Nicholas",
                  keywords: ["semantic-selectors"],
                  href: "https://apps-nicholas.acro.surfly.com/semantic-selectors/",
                },
                {
                  id: "app:atlas-tony/deploy-calendar",
                  label: "Deploy Calendar",
                  description: "deploy-calendar · Tony",
                  href: "https://apps-tony.acro.surfly.com/deploy-calendar/",
                },
              ],
            },
            {
              id: "tools",
              label: "Company tools",
              items: async () => [
                {
                  id: "link:drata",
                  label: "Drata",
                  description: "Compliance and audits",
                  keywords: ["compliance", "soc2"],
                  href: "https://app.drata.com/",
                },
              ],
            },
          ],
        },
      },
    ],
  }),
}));

beforeAll(() => {
  // cmdk scrolls the selected item into view; happy-dom has no layout.
  if (!("scrollIntoView" in Element.prototype))
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      value: () => undefined,
      configurable: true,
    });
});

function renderPalette() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <I18nContext.Provider
        value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
      >
        <CommandPalette />
      </I18nContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  act(() => setCommandPaletteOpen(false));
  cleanup();
  push.mockReset();
  rs.restoreAllMocks();
});

describe("CommandPalette (DOM)", () => {
  it("finds the project by name and the app by its slug", async () => {
    renderPalette();
    act(() => setCommandPaletteOpen(true));
    await screen.findByText("Deploy Calendar");
    fireEvent.change(
      screen.getByPlaceholderText(enUS.shortcuts.searchActions),
      {
        target: { value: "sem" },
      },
    );
    await waitFor(() => {
      expect(screen.queryByText("Deploy Calendar")).toBeNull();
      expect(screen.queryByText("Office Reminders")).toBeNull();
    });
    expect(screen.getByText("Semantic Selectors")).toBeTruthy();
    expect(
      screen.getByText("STR-2026-0814: Resilient Web Element Relocalization"),
    ).toBeTruthy();
  });

  it("opens a project in the app and an app in a new tab", async () => {
    const open = rs.spyOn(window, "open").mockImplementation(() => null);
    renderPalette();
    act(() => setCommandPaletteOpen(true));
    fireEvent.click(await screen.findByText("Deploy Calendar"));
    expect(open).toHaveBeenCalledWith(
      "https://apps-tony.acro.surfly.com/deploy-calendar/",
      "_blank",
      "noopener,noreferrer",
    );
    act(() => setCommandPaletteOpen(true));
    fireEvent.click(await screen.findByText("Semantic Selectors"));
    expect(push).toHaveBeenCalledWith("/workspace/projects/p1");
  });

  it("lists Apps before Projects, tools after them, actions last", async () => {
    renderPalette();
    act(() => setCommandPaletteOpen(true));
    await screen.findByText("Drata");
    const headings = Array.from(
      document.querySelectorAll("[cmdk-group-heading]"),
    ).map((heading) => heading.textContent);
    expect(headings).toEqual([
      "Apps",
      enUS.shortcuts.projects,
      "Company tools",
      enUS.shortcuts.actions,
    ]);
  });

  it("puts an exact tool match first and selects it", async () => {
    renderPalette();
    act(() => setCommandPaletteOpen(true));
    await screen.findByText("Drata");
    fireEvent.change(
      screen.getByPlaceholderText(enUS.shortcuts.searchActions),
      {
        target: { value: "drata" },
      },
    );
    await waitFor(() =>
      expect(screen.queryByText("Deploy Calendar")).toBeNull(),
    );
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "DrataCompliance and audits",
    ]);
    expect(options[0]!.getAttribute("aria-selected")).toBe("true");
  });
});
