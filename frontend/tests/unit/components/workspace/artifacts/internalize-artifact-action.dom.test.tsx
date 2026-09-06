import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";

import { InternalizeArtifactAction } from "@/components/workspace/artifacts/internalize-artifact-action";
import { ThreadContext } from "@/components/workspace/messages/context";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";
import type { AgentThreadState } from "@/core/threads/types";

rs.mock("@/core/static-mode", () => ({
  isStaticWebsiteOnly: () => false,
}));

const THREAD_ID = "aa5151dc-0000-4000-8000-000000000002";
const FILE = "/mnt/user-data/outputs/report.md";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockStatus(shared: boolean) {
  rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
    json({
      shared,
      share: shared
        ? {
            id: 1,
            rel_path: "outputs/report.md",
            title: "report.md",
            bytes: 1200,
            content_type: "text/markdown",
            scrub_findings: 0,
            shared_at: "2026-09-06T09:12:00+00:00",
            revoked_at: null,
          }
        : null,
      share_url: shared
        ? "https://agora.example/files/shared/atlas-test/x/outputs/report.md"
        : null,
    }),
  );
}

function fakeThread(): BaseStream<AgentThreadState> {
  const messages: Message[] = [
    { type: "human", id: "m0", content: "hi" } as Message,
  ];
  return {
    messages,
    values: { title: "t", messages },
    isLoading: false,
    isThreadLoading: false,
    error: undefined,
    getMessagesMetadata: () => undefined,
  } as unknown as BaseStream<AgentThreadState>;
}

function renderAction(variant: "card" | "detail") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <QueryClientProvider client={client}>
        <ThreadContext.Provider value={{ thread: fakeThread(), isMock: false }}>
          <InternalizeArtifactAction
            threadId={THREAD_ID}
            filepath={FILE}
            variant={variant}
          />
        </ThreadContext.Provider>
      </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

afterEach(cleanup);
afterEach(() => {
  rs.restoreAllMocks();
});

describe("InternalizeArtifactAction (DOM)", () => {
  it("renders the card variant icon-only with an accessible name", async () => {
    mockStatus(false);
    renderAction("card");
    const button = await screen.findByTestId("internalize-artifact-card");
    expect(button.getAttribute("aria-label")).toBe(
      enUS.sharing.artifact.internalize,
    );
    expect(button.textContent?.trim()).toBe("");
    expect(button.querySelector("svg")).not.toBeNull();
    expect(button.getAttribute("aria-pressed")).toBe("false");
  });

  it("marks the shared state on the glyph and the button", async () => {
    mockStatus(true);
    renderAction("card");
    const button = await screen.findByRole("button", {
      name: enUS.sharing.artifact.shared,
    });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.querySelector("svg")?.getAttribute("data-shared")).toBe(
      "true",
    );
  });
});
