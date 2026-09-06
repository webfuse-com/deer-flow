import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { SidebarProvider } from "@/components/ui/sidebar";
import { ChatProviders } from "@/components/workspace/chats/chat-providers";
import { SharedThreadPage } from "@/components/workspace/shared/shared-thread-page";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";

import fixture from "../../../fixtures/shared-thread-snapshot.json";

rs.mock("next/navigation", () => ({
  useRouter: () => ({ push: rs.fn(), replace: rs.fn(), refresh: rs.fn() }),
  usePathname: () => "/workspace/shared/atlas-nicholas/aa5151dc",
  useParams: () => ({}),
  useSearchParams: () => new URLSearchParams(),
}));

rs.mock("@/core/static-mode", () => ({
  isStaticWebsiteOnly: () => false,
}));

// The right panel (artifact detail, browser view) is layout machinery that
// happy-dom cannot measure; the page under test is the read-only transcript.
rs.mock("@/components/workspace/chats/chat-box", () => ({
  ChatBox: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <AuthProvider initialUser={null}>
        <QueryClientProvider client={client}>
          <SidebarProvider defaultOpen={false}>
            <ChatProviders>
              <SharedThreadPage
                stack="atlas-nicholas"
                threadId="aa5151dc-0000-4000-8000-000000000001"
              />
            </ChatProviders>
          </SidebarProvider>
        </QueryClientProvider>
      </AuthProvider>
    </I18nContext.Provider>,
  );
}

afterEach(cleanup);
afterEach(() => {
  rs.restoreAllMocks();
});

describe("SharedThreadPage (DOM)", () => {
  it("renders the snapshot with the real message components, read-only", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(
      async (input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (url.includes("/api/shared-threads/atlas-nicholas/")) {
          return json(fixture);
        }
        return json({ detail: "not mocked" }, 404);
      },
    );
    renderPage();

    await screen.findByText("How many seats did we sell in Q2?");
    await waitFor(() => {
      expect(document.body.textContent).toContain("1,240 seats");
    });
    // The tool step is still listed (bash renders as "Execute command") even
    // though its body was left out; the collapsed step keeps the placeholder
    // out of the visible text, and the notice line says so instead.
    expect(document.body.textContent).toContain("Execute command");

    // Header: who shared it, the title, the copy-link control.
    expect(screen.getByText("Shared by nicholas@surfly.com")).toBeTruthy();
    expect(screen.getByTestId("shared-thread-title").textContent).toBe(
      "Q2 seat forecast",
    );
    expect(screen.getByRole("button", { name: "Copy link" })).toBeTruthy();

    // Read-only: no input, no regenerate or edit affordances.
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.queryByRole("button", { name: /regenerate/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.getByTestId("shared-thread-notice").textContent).toContain(
      "Tool results left out.",
    );
  });

  it("shows the not-found state when the share is gone", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      json({ detail: "no such shared thread", code: "not_shared" }, 404),
    );
    renderPage();
    await screen.findByText("This shared conversation is not available");
    expect(
      screen.getByText(
        "The link may be wrong, or its owner stopped sharing it.",
      ),
    ).toBeTruthy();
  });
});
