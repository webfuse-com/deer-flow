import { beforeEach, expect, test, rs } from "@rstest/core";

const fetchWithAuth = rs.fn();

rs.mock("@/core/api/fetcher", () => ({
  fetch: fetchWithAuth,
}));

beforeEach(() => {
  fetchWithAuth.mockReset();
});

test("fetchThreadTokenUsage uses shared auth fetch without JSON GET headers", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: true,
    json: async () => ({
      thread_id: "thread-1",
      total_input_tokens: 3,
      total_output_tokens: 4,
      total_tokens: 7,
      total_runs: 1,
      by_model: { unknown: { tokens: 7, runs: 1 } },
      by_caller: {
        lead_agent: 0,
        subagent: 0,
        middleware: 0,
      },
    }),
  });

  const { fetchThreadTokenUsage } = await import("@/core/threads/api");

  await expect(fetchThreadTokenUsage("thread-1")).resolves.toMatchObject({
    thread_id: "thread-1",
    total_tokens: 7,
  });

  expect(fetchWithAuth).toHaveBeenCalledWith(
    expect.stringContaining("/api/threads/thread-1/token-usage"),
    {
      method: "GET",
    },
  );
});

test("fetchThreadTokenUsage returns null for unavailable token usage", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: false,
    status: 404,
  });

  const { fetchThreadTokenUsage } = await import("@/core/threads/api");

  await expect(fetchThreadTokenUsage("thread-1")).resolves.toBeNull();
});

test("acquireDebugSandbox POSTs and returns hash + url", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: true,
    json: async () => ({ hash: "fc7b6e5e", url: "/debug-sandbox/fc7b6e5e/" }),
  });

  const { acquireDebugSandbox } = await import("@/core/threads/api");

  await expect(acquireDebugSandbox("thread-1")).resolves.toEqual({
    hash: "fc7b6e5e",
    url: "/debug-sandbox/fc7b6e5e/",
  });

  expect(fetchWithAuth).toHaveBeenCalledWith(
    expect.stringContaining("/api/threads/thread-1/debug-sandbox"),
    {
      method: "POST",
    },
  );
});

test("acquireDebugSandbox returns null when provider is not container-backed (409)", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: false,
    status: 409,
  });

  const { acquireDebugSandbox } = await import("@/core/threads/api");

  await expect(acquireDebugSandbox("thread-1")).resolves.toBeNull();
});

test("acquireDebugSandbox throws on unexpected error status", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: false,
    status: 502,
  });

  const { acquireDebugSandbox } = await import("@/core/threads/api");

  await expect(acquireDebugSandbox("thread-1")).rejects.toThrow();
});
