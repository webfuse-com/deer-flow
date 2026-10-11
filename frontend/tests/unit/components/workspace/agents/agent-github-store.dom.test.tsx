/**
 * [argus patch #110] The Agents page edits the description and soul, links
 * to the GitHub copy the agent store reports, shows store refusals as the
 * server's own text, and creates agents again from the gallery.
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

import { AgentCard } from "@/components/workspace/agents/agent-card";
import { AgentCreateDialog } from "@/components/workspace/agents/agent-create-dialog";
import { AgentSettingsDialog } from "@/components/workspace/agents/agent-settings-dialog";
import type { Agent } from "@/core/agents";
import { enUS } from "@/core/i18n/locales/en-US";
import type { Model } from "@/core/models/types";

const mocks = rs.hoisted(() => ({
  updateMutateAsync: rs.fn(),
  createMutateAsync: rs.fn(),
  checkAgentName: rs.fn(),
}));

rs.mock("@/core/agents", () => {
  class AgentNameCheckError extends Error {
    constructor(
      message: string,
      public readonly reason: "backend_unreachable" | "request_failed",
      public readonly detail: string | null = null,
    ) {
      super(message);
      this.name = "AgentNameCheckError";
    }
  }
  class AgentsApiDisabledError extends Error {}
  return {
    useUpdateAgent: () => ({
      mutateAsync: mocks.updateMutateAsync,
      isPending: false,
    }),
    useCreateAgent: () => ({
      mutateAsync: mocks.createMutateAsync,
      isPending: false,
    }),
    useDeleteAgent: () => ({ mutateAsync: rs.fn(), isPending: false }),
    exportAgentPackage: rs.fn(),
    checkAgentName: mocks.checkAgentName,
    AgentNameCheckError,
    AgentsApiDisabledError,
  };
});
rs.mock("@/core/features", () => ({
  useKnowledgeBaseEnabled: () => ({ scopeSelectionEnabled: false }),
}));
rs.mock("@/core/models/hooks", () => ({
  useModels: () => ({
    models: [
      {
        id: "qwen",
        name: "qwen",
        model: "qwen",
        display_name: "Qwen",
      } satisfies Model,
    ],
  }),
}));
rs.mock("@/core/subagents", () => ({
  useSubagents: () => ({ subagents: [] }),
}));
rs.mock("@/core/capabilities/hooks", () => ({
  useCapabilityInstallations: () => ({
    data: { items: [], can_manage: false },
    isLoading: false,
    isError: false,
  }),
}));
rs.mock("@/core/i18n/hooks", () => ({ useI18n: () => ({ t: enUS }) }));
rs.mock("sonner", () => ({ toast: { success: rs.fn(), error: rs.fn() } }));
rs.mock("next/navigation", () => ({ useRouter: () => ({ push: rs.fn() }) }));
// CodeMirror is heavy and irrelevant here: a textarea stands in for the editor.
rs.mock("@/components/workspace/code-editor", () => ({
  CodeEditor: ({
    value,
    onChange,
  }: {
    value: string;
    onChange?: (value: string) => void;
  }) => (
    <textarea
      aria-label="Soul editor"
      value={value}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));

const agent: Agent = {
  name: "atlas",
  description: "Answers questions",
  model: null,
  tool_groups: null,
  skills: null,
  soul: "# Atlas\n\nBe direct.",
};

const githubSource = {
  kind: "github" as const,
  url: "https://github.com/example/atlas-demo/tree/main/agents/atlas",
  path: "agents/atlas",
  commit: "0123456789abcdef",
};

function lastUpdateRequest() {
  const call = mocks.updateMutateAsync.mock.calls.at(-1)![0] as {
    name: string;
    request: Record<string, unknown>;
  };
  return call.request;
}

afterEach(() => {
  cleanup();
  mocks.updateMutateAsync.mockReset();
  mocks.createMutateAsync.mockReset();
  mocks.checkAgentName.mockReset();
  rs.mocked(toast.success).mockClear();
  rs.mocked(toast.error).mockClear();
});

describe("agent settings: description and soul", () => {
  it("shows the soul and saves an edited soul", async () => {
    mocks.updateMutateAsync.mockResolvedValue({});
    render(<AgentSettingsDialog agent={agent} open onOpenChange={rs.fn()} />);
    const editor = screen.getByRole("textbox", { name: "Soul editor" });
    expect((editor as HTMLTextAreaElement).value).toBe(agent.soul);
    fireEvent.change(editor, { target: { value: "# Atlas\n\nBe brief." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateMutateAsync).toHaveBeenCalled());
    const request = lastUpdateRequest();
    expect(request.soul).toBe("# Atlas\n\nBe brief.");
    expect(request).not.toHaveProperty("description");
    expect(toast.success).toHaveBeenCalledWith(enUS.agents.settingsSaved);
  });

  it("saves an edited description without resending the soul", async () => {
    mocks.updateMutateAsync.mockResolvedValue({});
    render(<AgentSettingsDialog agent={agent} open onOpenChange={rs.fn()} />);
    const field = screen.getByLabelText("Description");
    expect((field as HTMLTextAreaElement).value).toBe("Answers questions");
    fireEvent.change(field, { target: { value: "  Plans the week  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateMutateAsync).toHaveBeenCalled());
    const request = lastUpdateRequest();
    expect(request.description).toBe("Plans the week");
    expect(request).not.toHaveProperty("soul");
  });

  it("leaves description and soul out of an untouched save", async () => {
    mocks.updateMutateAsync.mockResolvedValue({});
    render(<AgentSettingsDialog agent={agent} open onOpenChange={rs.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateMutateAsync).toHaveBeenCalled());
    const request = lastUpdateRequest();
    expect(request).not.toHaveProperty("description");
    expect(request).not.toHaveProperty("soul");
  });

  it("shows the store's refusal text and stays open", async () => {
    const detail =
      "Changed on GitHub since this page loaded (abc1234). Reload and try again.";
    mocks.updateMutateAsync.mockRejectedValue(new Error(detail));
    const onOpenChange = rs.fn();
    render(
      <AgentSettingsDialog agent={agent} open onOpenChange={onOpenChange} />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Soul editor" }), {
      target: { value: "edited" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(detail));
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("toggles the soul editor full screen without losing the draft", () => {
    render(<AgentSettingsDialog agent={agent} open onOpenChange={rs.fn()} />);
    const dialog = screen.getByTestId("agent-settings-dialog");
    expect(dialog.getAttribute("data-expanded")).toBe("false");
    fireEvent.change(screen.getByRole("textbox", { name: "Soul editor" }), {
      target: { value: "draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Open full screen" }));
    expect(dialog.getAttribute("data-expanded")).toBe("true");
    const editors = screen.getAllByRole("textbox", { name: "Soul editor" });
    expect(editors).toHaveLength(1);
    expect((editors[0] as HTMLTextAreaElement).value).toBe("draft");
    fireEvent.click(screen.getByRole("button", { name: "Exit full screen" }));
    expect(dialog.getAttribute("data-expanded")).toBe("false");
  });
});

describe("GitHub source link", () => {
  it("links the settings dialog to GitHub with the short commit", async () => {
    mocks.updateMutateAsync.mockResolvedValue({
      ...agent,
      source: githubSource,
    });
    render(
      <AgentSettingsDialog
        agent={{ ...agent, source: githubSource }}
        open
        onOpenChange={rs.fn()}
      />,
    );
    const link = screen.getByRole("link", { name: /Open on GitHub/ });
    expect(link.getAttribute("href")).toBe(githubSource.url);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
    expect(within(link).getByText("0123456")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Saved to GitHub"),
    );
  });

  it("shows no link in the settings dialog without a source", () => {
    render(<AgentSettingsDialog agent={agent} open onOpenChange={rs.fn()} />);
    expect(screen.queryByRole("link", { name: /Open on GitHub/ })).toBeNull();
  });

  it("puts a GitHub link on the card only when the store reports one", () => {
    const { rerender } = render(
      <AgentCard agent={{ ...agent, source: githubSource }} />,
    );
    const link = screen.getByRole("link", { name: "Open on GitHub" });
    expect(link.getAttribute("href")).toBe(githubSource.url);
    expect(link.getAttribute("target")).toBe("_blank");
    rerender(<AgentCard agent={agent} />);
    expect(screen.queryByRole("link", { name: "Open on GitHub" })).toBeNull();
    rerender(<AgentCard agent={{ ...agent, source: null }} />);
    expect(screen.queryByRole("link", { name: "Open on GitHub" })).toBeNull();
  });
});

describe("New agent dialog", () => {
  it("refuses an invalid name before asking the server", () => {
    render(<AgentCreateDialog open onOpenChange={rs.fn()} />);
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "code reviewer!" },
    });
    expect(screen.getByRole("alert").textContent).toBe(
      enUS.agents.createNameInvalid,
    );
    const submit = screen.getByRole("button", { name: "Create agent" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.click(submit);
    expect(mocks.checkAgentName).not.toHaveBeenCalled();
    expect(mocks.createMutateAsync).not.toHaveBeenCalled();
  });

  it("reports a taken name and does not create", async () => {
    mocks.checkAgentName.mockResolvedValue({
      available: false,
      name: "atlas",
    });
    render(<AgentCreateDialog open onOpenChange={rs.fn()} />);
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "atlas" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      enUS.agents.nameStepAlreadyExistsError,
    );
    expect(mocks.createMutateAsync).not.toHaveBeenCalled();
  });

  it("creates the agent with a lowercase name and closes", async () => {
    mocks.checkAgentName.mockResolvedValue({
      available: true,
      name: "code-reviewer",
    });
    mocks.createMutateAsync.mockResolvedValue({
      ...agent,
      name: "code-reviewer",
      source: githubSource,
    });
    const onOpenChange = rs.fn();
    render(<AgentCreateDialog open onOpenChange={onOpenChange} />);
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Code-Reviewer" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Reviews pull requests" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Soul editor" }), {
      target: { value: "# Reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() =>
      expect(mocks.createMutateAsync).toHaveBeenCalledWith({
        name: "code-reviewer",
        description: "Reviews pull requests",
        model: "qwen",
        soul: "# Reviewer",
      }),
    );
    expect(mocks.checkAgentName).toHaveBeenCalledWith("Code-Reviewer");
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(toast.success).toHaveBeenCalledWith(enUS.agents.createdOnGithub);
  });

  it("shows the store's refusal text when creating fails", async () => {
    mocks.checkAgentName.mockResolvedValue({ available: true, name: "x" });
    mocks.createMutateAsync.mockRejectedValue(
      new Error("Not saved to GitHub: rate limited"),
    );
    const onOpenChange = rs.fn();
    render(<AgentCreateDialog open onOpenChange={onOpenChange} />);
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Not saved to GitHub: rate limited",
      ),
    );
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
