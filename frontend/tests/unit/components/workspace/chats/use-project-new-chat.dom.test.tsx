// [argus patch #122] The project-scoped new chat, shared by the plain chat
// page and the agent chat page (Argus opens new chats on the Atlas agent page).
import { afterEach, beforeEach, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";

const mocks = rs.hoisted(() => ({
  search: "",
  createThread: rs.fn(),
  toastError: rs.fn(),
}));
rs.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(mocks.search),
}));
rs.mock("@/core/threads/api", () => ({ createThread: mocks.createThread }));
rs.mock("@/core/threads/hooks", () => ({
  INFINITE_THREADS_QUERY_KEY_PREFIX: ["threads", "infinite"],
}));
rs.mock("@/core/i18n/hooks", () => ({
  useI18n: () => ({
    t: { projects: { projectUnavailable: "Project unavailable" } },
  }),
}));
rs.mock("sonner", () => ({ toast: { error: mocks.toastError } }));

import { useProjectNewChat } from "@/components/workspace/chats/use-project-new-chat";

let client: QueryClient;
function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  client = new QueryClient();
  mocks.search = "";
  mocks.createThread.mockReset();
  mocks.toastError.mockReset();
});
afterEach(() => {
  cleanup();
  client.clear();
});

test("a new project chat files its first message under the project", async () => {
  mocks.search = "project=p1";
  const created = { thread_id: "t1", metadata: { deerflow_project_id: "p1" } };
  mocks.createThread.mockResolvedValue(created);
  const { result } = renderHook(
    () => useProjectNewChat({ threadId: "t1", isNewThread: true }),
    { wrapper },
  );
  expect(result.current.projectParam).toBe("p1");
  await result.current.ensureProjectThread();
  expect(mocks.createThread).toHaveBeenCalledWith("t1", "p1");
  expect(client.getQueryData(["thread", "metadata", "t1", false])).toEqual(
    created,
  );
});

test("an existing chat ignores the project parameter", async () => {
  mocks.search = "project=p1";
  const { result } = renderHook(
    () => useProjectNewChat({ threadId: "t1", isNewThread: false }),
    { wrapper },
  );
  expect(result.current.projectParam).toBeNull();
  await result.current.ensureProjectThread();
  expect(mocks.createThread).not.toHaveBeenCalled();
});

test("an unavailable project refuses the send and says so", async () => {
  mocks.search = "project=gone";
  mocks.createThread.mockRejectedValue(new Error("404"));
  const { result } = renderHook(
    () => useProjectNewChat({ threadId: "t1", isNewThread: true }),
    { wrapper },
  );
  await expect(result.current.ensureProjectThread()).rejects.toThrow("404");
  expect(mocks.toastError).toHaveBeenCalledWith("Project unavailable");
});

test("switching conversation or project scope moves the submission fence", () => {
  mocks.search = "project=p1";
  const { result, rerender } = renderHook(
    ({ threadId }) => useProjectNewChat({ threadId, isNewThread: true }),
    { wrapper, initialProps: { threadId: "t1" } },
  );
  const fence = result.current.submissionEpochRef;
  const submissionEpoch = fence.current;
  rerender({ threadId: "t1" });
  expect(fence.current).toBe(submissionEpoch);
  rerender({ threadId: "t2" });
  expect(fence.current).not.toBe(submissionEpoch);
  const afterSwitch = fence.current;
  mocks.search = "";
  rerender({ threadId: "t2" });
  expect(fence.current).not.toBe(afterSwitch);
});
