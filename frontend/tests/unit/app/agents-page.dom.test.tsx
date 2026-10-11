/**
 * [argus patch #112] Plugins add tabs to the Agents page through the
 * `agents-tab` surface slot, mounted like a project tab. Without any the
 * page has no tab strip.
 */
import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import AgentsPage from "@/app/workspace/agents/page";
import type {
  PluginSurface,
  SurfaceContext,
} from "@/core/extensions/contracts";
import type { LoadedContribution } from "@/core/extensions/registry";
import { enUS } from "@/core/i18n/locales/en-US";

const state = rs.hoisted(() => ({
  entries: [] as LoadedContribution[],
  // Stable like Next's router: a new object per render would remount surfaces.
  router: { push: () => undefined, replace: () => undefined },
}));

rs.mock("@/core/extensions/hooks", () => ({
  useFrontendExtensions: () => ({ data: state.entries }),
  useFrontendServices: () => ({
    conversationText: async () => "",
    showMessage: () => undefined,
  }),
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: "user-1" } }),
}));
rs.mock("@/core/i18n/hooks", () => ({
  useI18n: () => ({ t: enUS, locale: "en-US" }),
}));
rs.mock("next/navigation", () => ({
  useRouter: () => state.router,
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "" }));
rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));
rs.mock("@/core/agents", () => ({
  useAgents: () => ({
    agents: [{ name: "atlas", description: "", soul: "" }],
    isLoading: false,
  }),
  useImportAgentPackage: () => ({ mutateAsync: rs.fn(), isPending: false }),
}));
rs.mock("@/components/workspace/agents/agent-card", () => ({
  AgentCard: ({ agent }: { agent: { name: string } }) => (
    <div data-testid="agent-card">{agent.name}</div>
  ),
}));
rs.mock("@/components/workspace/agents/agent-create-dialog", () => ({
  AgentCreateDialog: () => null,
}));

const dispose = rs.fn();
const mount = rs.fn((root: HTMLElement, _context: SurfaceContext) => {
  root.textContent = "CODING AGENTS UI";
  return { dispose };
});

const codingTab: PluginSurface = {
  id: "coding-agents",
  slot: "agents-tab",
  title: "Coding agents",
  mount,
};
const projectTab: PluginSurface = {
  id: "details",
  slot: "project-tab",
  title: "Details",
  mount: () => ({ dispose: () => undefined }),
};

function plugin(
  namespace: string,
  surfaces: PluginSurface[],
  enabled = true,
): LoadedContribution {
  return {
    namespace,
    module: `${namespace}.v1`,
    entry: null,
    title: namespace,
    description: "",
    settings: { enabled, mode: "test" },
    backend_actions: ["settings"],
    extension: { apiVersion: 1, module: `${namespace}.v1`, surfaces },
  };
}

beforeEach(() => {
  state.entries = [];
});

afterEach(() => {
  cleanup();
  rs.clearAllMocks();
});

describe("AgentsPage plugin tabs", () => {
  it("shows the gallery tab first, then the plugin tab, and mounts it without a project", () => {
    state.entries = [
      plugin("argus", [projectTab, codingTab]),
      plugin("off", [{ ...codingTab, title: "Disabled tab" }], false),
    ];
    render(<AgentsPage />);

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Agents",
      "Coding agents",
    ]);
    expect(screen.getByTestId("agent-card").textContent).toBe("atlas");
    expect(mount).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Coding agents" }));

    expect(screen.queryByTestId("agent-card")).toBeNull();
    expect(mount).toHaveBeenCalledTimes(1);
    const [root, context] = mount.mock.calls[0]!;
    expect(context).toMatchObject({
      namespace: "argus",
      locale: "en-US",
      settings: { enabled: true, mode: "test" },
    });
    expect(context.project).toBeUndefined();
    expect(context.threadId).toBeUndefined();
    expect(typeof context.callBackend).toBe("function");
    expect(context.signal.aborted).toBe(false);
    // Same Shadow DOM mount as a plugin page or project tab.
    const section = screen.getByRole("region", { name: "Coding agents" });
    const host = section.querySelector("div")!;
    expect(host.shadowRoot?.contains(root)).toBe(true);
    expect(host.shadowRoot?.textContent).toBe("CODING AGENTS UI");
    // Import and New act on the gallery only.
    expect(
      screen
        .getByRole("button", { name: "New Agent" })
        .parentElement?.className.split(" "),
    ).toContain("hidden");

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Agents" }));
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(context.signal.aborted).toBe(true);
    expect(screen.getByTestId("agent-card").textContent).toBe("atlas");
  });

  it("has no tab strip when no enabled plugin contributes an agents tab", () => {
    state.entries = [
      plugin("argus", [projectTab]),
      plugin("off", [codingTab], false),
    ];
    render(<AgentsPage />);

    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.getByTestId("agent-card").textContent).toBe("atlas");
    expect(
      screen
        .getByRole("button", { name: "New Agent" })
        .parentElement?.className.split(" "),
    ).not.toContain("hidden");
    expect(mount).not.toHaveBeenCalled();
  });

  it("falls back to the gallery when the selected plugin tab goes away", () => {
    state.entries = [plugin("argus", [codingTab])];
    const { rerender } = render(<AgentsPage />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Coding agents" }));
    expect(mount).toHaveBeenCalledTimes(1);

    state.entries = [];
    rerender(<AgentsPage />);

    expect(dispose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByTestId("agent-card").textContent).toBe("atlas");
  });
});
