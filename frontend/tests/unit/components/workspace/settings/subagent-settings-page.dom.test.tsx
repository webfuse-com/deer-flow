/**
 * [argus patch #111] Managed subagents get a Thinking switch, a link to the
 * GitHub copy their store reports, and the store's refusals as the server's
 * own text.
 */
import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { toast } from "sonner";

import { SubagentSettingsPage } from "@/components/workspace/settings/subagent-settings-page";
import { enUS } from "@/core/i18n/locales/en-US";
import type { Subagent } from "@/core/subagents";

const mocks = rs.hoisted(() => ({
  subagents: [] as unknown[],
  createMutateAsync: rs.fn(),
  updateMutateAsync: rs.fn(),
  deleteMutateAsync: rs.fn(),
}));

rs.mock("@/core/subagents", () => ({
  useSubagents: () => ({
    subagents: mocks.subagents,
    isLoading: false,
    error: null,
  }),
  useCreateManagedSubagent: () => ({
    mutateAsync: mocks.createMutateAsync,
    isPending: false,
  }),
  useUpdateManagedSubagent: () => ({
    mutateAsync: mocks.updateMutateAsync,
    isPending: false,
  }),
  useDeleteManagedSubagent: () => ({
    mutateAsync: mocks.deleteMutateAsync,
    isPending: false,
  }),
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: "admin-id", system_role: "admin" } }),
}));
rs.mock("@/core/models/hooks", () => ({ useModels: () => ({ models: [] }) }));
rs.mock("@/core/i18n/hooks", () => ({ useI18n: () => ({ t: enUS }) }));
rs.mock("sonner", () => ({ toast: { success: rs.fn(), error: rs.fn() } }));

const planner: Subagent = {
  name: "planner",
  display_name: null,
  description: "Plans the work",
  system_prompt: "You plan.",
  tools: null,
  disallowed_tools: null,
  skills: null,
  model: "inherit",
  max_turns: 50,
  timeout_seconds: 900,
  enabled: true,
  thinking_enabled: null,
  source: "managed",
  editable: true,
  conflict: false,
  config_overrides: {},
};

const githubStoredIn = {
  kind: "github" as const,
  url: "https://github.com/example/atlas-demo/blob/main/subagents/planner.yaml",
  path: "subagents/planner.yaml",
  commit: "0123456789abcdef",
};

function showSubagents(...subagents: Subagent[]) {
  mocks.subagents = subagents;
  render(<SubagentSettingsPage />);
}

function thinkingSwitch() {
  return screen.getByRole("switch", { name: enUS.settings.subagents.thinking });
}

function lastUpdateRequest() {
  const call = mocks.updateMutateAsync.mock.calls.at(-1)![0] as {
    name: string;
    request: Record<string, unknown>;
  };
  return call.request;
}

afterEach(() => {
  cleanup();
  mocks.subagents = [];
  mocks.createMutateAsync.mockReset();
  mocks.updateMutateAsync.mockReset();
  mocks.deleteMutateAsync.mockReset();
  rs.mocked(toast.success).mockClear();
  rs.mocked(toast.error).mockClear();
});

describe("Thinking switch", () => {
  it("shows the stored value and saves a change", async () => {
    mocks.updateMutateAsync.mockResolvedValue({});
    showSubagents({ ...planner, thinking_enabled: true });
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(thinkingSwitch().getAttribute("aria-checked")).toBe("true");
    fireEvent.click(thinkingSwitch());
    expect(thinkingSwitch().getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateMutateAsync).toHaveBeenCalled());
    expect(mocks.updateMutateAsync.mock.calls.at(-1)![0].name).toBe("planner");
    expect(lastUpdateRequest().thinking_enabled).toBe(false);
    expect(toast.success).toHaveBeenCalledWith(enUS.settings.subagents.saved);
  });

  it("leaves thinking out of an untouched save", async () => {
    mocks.updateMutateAsync.mockResolvedValue({});
    showSubagents(planner);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(thinkingSwitch().getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateMutateAsync).toHaveBeenCalled());
    expect(lastUpdateRequest()).not.toHaveProperty("thinking_enabled");
  });

  it("creates a subagent with thinking turned on", async () => {
    mocks.createMutateAsync.mockResolvedValue({});
    showSubagents();
    fireEvent.click(
      screen.getByRole("button", { name: enUS.settings.subagents.create }),
    );
    // The new-subagent name label also holds its hint text.
    fireEvent.change(screen.getByLabelText(/^Name/), {
      target: { value: "reviewer" },
    });
    fireEvent.change(
      screen.getByLabelText(enUS.settings.subagents.descriptionLabel),
      { target: { value: "Reviews changes" } },
    );
    fireEvent.change(
      screen.getByLabelText(enUS.settings.subagents.systemPrompt),
      { target: { value: "You review." } },
    );
    expect(thinkingSwitch().getAttribute("aria-checked")).toBe("false");
    fireEvent.click(thinkingSwitch());
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.createMutateAsync).toHaveBeenCalled());
    expect(mocks.createMutateAsync.mock.calls.at(-1)![0]).toMatchObject({
      name: "reviewer",
      description: "Reviews changes",
      system_prompt: "You review.",
      thinking_enabled: true,
    });
  });
});

describe("GitHub link", () => {
  it("links a stored subagent to GitHub with the short commit", () => {
    showSubagents({ ...planner, stored_in: githubStoredIn });
    const link = screen.getByRole("link", { name: /Open on GitHub/ });
    expect(link.getAttribute("href")).toBe(githubStoredIn.url);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
    expect(within(link).getByText("0123456")).toBeTruthy();
  });

  it("shows the link in the edit dialog too", () => {
    showSubagents({ ...planner, stored_in: githubStoredIn });
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog");
    const link = within(dialog).getByRole("link", { name: /Open on GitHub/ });
    expect(link.getAttribute("href")).toBe(githubStoredIn.url);
  });

  it("shows no link without a stored location", () => {
    showSubagents(planner, {
      ...planner,
      name: "reviewer",
      stored_in: null,
    });
    expect(screen.queryByRole("link", { name: /Open on GitHub/ })).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0]!);
    expect(
      within(screen.getByRole("dialog")).queryByRole("link", {
        name: /Open on GitHub/,
      }),
    ).toBeNull();
  });
});

describe("store refusals", () => {
  it("shows the server's text when saving fails and stays open", async () => {
    const detail =
      "Changed on GitHub since this page loaded (abc1234). Reload and try again.";
    mocks.updateMutateAsync.mockRejectedValue(new Error(detail));
    showSubagents({ ...planner, stored_in: githubStoredIn });
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(thinkingSwitch());
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(detail));
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("shows the server's text when deleting fails", async () => {
    const confirm = rs.spyOn(window, "confirm").mockReturnValue(true);
    mocks.deleteMutateAsync.mockRejectedValue(
      new Error("Not saved to GitHub: rate limited"),
    );
    showSubagents(planner);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Not saved to GitHub: rate limited",
      ),
    );
    expect(mocks.deleteMutateAsync).toHaveBeenCalledWith("planner");
    confirm.mockRestore();
  });
});
