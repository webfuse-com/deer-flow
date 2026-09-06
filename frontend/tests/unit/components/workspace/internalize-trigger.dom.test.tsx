import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { InternalizeTrigger } from "@/components/workspace/internalize-trigger";
import { ThreadContext } from "@/components/workspace/messages/context";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";
import type { AgentThreadState } from "@/core/threads/types";

rs.mock("@/core/static-mode", () => ({
  isStaticWebsiteOnly: () => false,
}));

const THREAD_ID = "aa5151dc-0000-4000-8000-000000000001";
const SHARE_URL = `https://agora.example/threads/shared/atlas-test/${THREAD_ID}`;

const notShared = {
  thread_id: THREAD_ID,
  stack: "atlas-test",
  shared: false,
  share: null,
  share_url: SHARE_URL,
  view_path: `/workspace/shared/atlas-test/${THREAD_ID}`,
};

const sharedStatus = {
  ...notShared,
  shared: true,
  share: {
    title: "Q2 seats",
    include_tool_bodies: false,
    snapshot_at: "2026-09-06T09:12:00+00:00",
    snapshot_seq: 412,
    snapshot_bytes: 48211,
    files_bytes: 20480,
    files_included: true,
    scrub_findings: 2,
    revoked_at: null,
  },
};

const preview = {
  title: "Q2 seats",
  source: "run_events",
  include_tool_bodies: false,
  counts: { human: 4, ai: 9, tool: 12 },
  text_bytes: 48211,
  omitted_tool_chars: 91234,
  scrub: { count: 2, kinds: { email: 2 } },
  files: {
    count: 3,
    bytes: 20480,
    included: true,
    list: [{ path: "outputs/report.md", bytes: 1200 }],
  },
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type Call = { url: string; method: string; body: unknown };

/** A tiny in-memory Agora: status flips on POST/DELETE, preview is static. */
function mockAgora({
  initiallyShared = false,
  previewStatus = 200,
  previewBody = preview as unknown,
}: {
  initiallyShared?: boolean;
  previewStatus?: number;
  previewBody?: unknown;
} = {}) {
  let shared = initiallyShared;
  const calls: Call[] = [];
  rs.spyOn(globalThis, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const method = init?.method ?? "GET";
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(init.body as string) : undefined,
      });
      if (url.includes("/preview")) {
        return json(previewBody, previewStatus);
      }
      if (url.includes("/api/shared-threads/mine/")) {
        if (method === "POST") {
          shared = true;
        } else if (method === "DELETE") {
          shared = false;
        }
        return json(shared ? sharedStatus : notShared);
      }
      return json({ detail: "unexpected" }, 404);
    },
  );
  return calls;
}

function fakeThread(messageCount: number) {
  const messages: Message[] = Array.from(
    { length: messageCount },
    (_, i) =>
      ({
        type: i % 2 === 0 ? "human" : "ai",
        id: `m${i}`,
        content: `message ${i}`,
      }) as Message,
  );
  return {
    messages,
    values: { title: "Q2 seats", messages },
    isLoading: false,
    isThreadLoading: false,
    error: undefined,
    getMessagesMetadata: () => undefined,
  } as unknown as BaseStream<AgentThreadState>;
}

function renderTrigger(messageCount = 2) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <QueryClientProvider client={client}>
        <ThreadContext.Provider
          value={{ thread: fakeThread(messageCount), isMock: false }}
        >
          <InternalizeTrigger threadId={THREAD_ID} />
        </ThreadContext.Provider>
      </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

afterEach(cleanup);
afterEach(() => {
  rs.restoreAllMocks();
});

describe("InternalizeTrigger (DOM)", () => {
  it("renders nothing for an empty thread", () => {
    mockAgora();
    renderTrigger(0);
    expect(screen.queryByTestId("internalize-trigger")).toBeNull();
  });

  it("shows the preview and scrub warning, then internalizes", async () => {
    const calls = mockAgora();
    renderTrigger();

    const button = await screen.findByRole("button", { name: "Internalize" });
    fireEvent.click(button);

    await screen.findByText("4 of your messages, 9 answers and 12 tool steps.");
    expect(
      screen.getByText("2 things look like a secret or a personal detail:"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Nothing is removed automatically; review before sharing.",
      ),
    ).toBeTruthy();

    // Toggling tool results re-previews with the flag set.
    fireEvent.click(
      screen.getByRole("switch", { name: "Include tool results" }),
    );
    await waitFor(() => {
      expect(
        calls.some((c) => c.url.endsWith("/preview?include_tool_bodies=1")),
      ).toBe(true);
    });

    const dialog = screen.getByRole("dialog");
    const confirm = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Internalize",
    );
    expect(confirm).toBeTruthy();
    await waitFor(() => expect(confirm!.hasAttribute("disabled")).toBe(false));
    fireEvent.click(confirm!);

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.url.endsWith(`/mine/${THREAD_ID}`),
      );
      expect(post?.body).toEqual({ include_tool_bodies: true });
    });

    // Shared state: the link and the stop button appear.
    const link = await screen.findByLabelText("Link for colleagues");
    expect((link as HTMLInputElement).value).toBe(SHARE_URL);
    expect(screen.getByRole("button", { name: "Stop sharing" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Refresh snapshot" }),
    ).toBeTruthy();
  });

  it("stops sharing after an inline confirmation", async () => {
    const calls = mockAgora({ initiallyShared: true });
    renderTrigger();

    const button = await screen.findByRole("button", {
      name: "Shared with colleagues",
    });
    fireEvent.click(button);

    fireEvent.click(
      await screen.findByRole("button", { name: "Stop sharing" }),
    );
    expect(
      screen.getByText(/The link stops working for everyone/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Yes, stop sharing" }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "DELETE")).toBe(true);
    });
    await screen.findByText("Share this conversation with your colleagues");
  });

  it("disables the confirm when the snapshot is too large", async () => {
    mockAgora({
      previewStatus: 422,
      previewBody: {
        detail: "this thread is 7.1 MB of text; the limit is 5 MB",
        code: "snapshot_too_large",
      },
    });
    renderTrigger();
    fireEvent.click(await screen.findByRole("button", { name: "Internalize" }));

    await screen.findByText(
      "This conversation is too large to share as a snapshot.",
    );
    const dialog = screen.getByRole("dialog");
    const confirm = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Internalize",
    );
    expect(confirm?.hasAttribute("disabled")).toBe(true);
  });

  it("offers a reload when the session has expired", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html>login</html>", {
        status: 200,
        headers: { "Content-Type": "text/html" },
      }),
    );
    renderTrigger();
    fireEvent.click(await screen.findByRole("button", { name: "Internalize" }));
    await screen.findByText("Your session has expired.");
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  });
});
