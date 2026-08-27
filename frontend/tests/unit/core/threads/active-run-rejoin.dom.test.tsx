import type { Run } from "@langchain/langgraph-sdk";
import { expect, rs, test } from "@rstest/core";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useActiveRunRejoin } from "@/core/threads/active-run-rejoin";

const clientMock = rs.hoisted(() => ({
  runsList: rs.fn(async () => [] as Run[]),
  remember: rs.fn(),
  joinStream: rs.fn(async () => undefined),
}));

// getAPIClient + rememberReconnectRun are imported by the hook from this module.
rs.mock("@/core/api/api-client", () => ({
  getAPIClient: () => ({ runs: { list: clientMock.runsList } }),
  rememberReconnectRun: clientMock.remember,
}));

function run(status: Run["status"], runId = `run-${status}`): Run {
  return {
    run_id: runId,
    thread_id: "t",
    assistant_id: "lead_agent",
    status,
    metadata: {},
    kwargs: {},
    multitask_strategy: "reject",
    created_at: "",
    updated_at: "",
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_tokens: 0,
    llm_call_count: 0,
    lead_agent_tokens: 0,
    subagent_tokens: 0,
    middleware_tokens: 0,
    message_count: 0,
    stop_reason: null,
  } as Run;
}

function renderRejoin(
  overrides: Partial<Parameters<typeof useActiveRunRejoin>[0]> = {},
) {
  return renderHook(
    (props: Parameters<typeof useActiveRunRejoin>[0]) =>
      useActiveRunRejoin(props),
    {
      initialProps: {
        threadId: "thread-1",
        joinStream: clientMock.joinStream,
        isLoading: false,
        isMock: false,
        ...overrides,
      },
    },
  );
}

test("rejoins the discovered active run and surfaces its id", async () => {
  const active = run("running", "run-active");
  clientMock.runsList.mockResolvedValue([active]);

  const { result } = renderRejoin();

  await waitFor(() => {
    expect(clientMock.joinStream).toHaveBeenCalledWith("run-active");
  });
  expect(clientMock.remember).toHaveBeenCalledWith("thread-1", "run-active");
  expect(result.current).toBe("run-active");
});

test("does nothing when the thread is idle (no active run)", async () => {
  clientMock.runsList.mockResolvedValue([run("success")]);
  clientMock.joinStream.mockClear();
  clientMock.remember.mockClear();

  const { result } = renderRejoin();

  // Let the discovery fetch settle (one macro task is enough).
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(clientMock.joinStream).not.toHaveBeenCalled();
  expect(clientMock.remember).not.toHaveBeenCalled();
  expect(result.current).toBeNull();
});

test("skips discovery while a stream is already loading", async () => {
  clientMock.runsList.mockClear();

  renderRejoin({ isLoading: true });

  await act(async () => {
    await Promise.resolve();
  });
  expect(clientMock.runsList).not.toHaveBeenCalled();
});

test("skips discovery when the SDK same-tab reconnect key is present", async () => {
  clientMock.runsList.mockClear();
  window.sessionStorage.setItem("lg:stream:thread-1", "known-run");

  renderRejoin();

  await act(async () => {
    await Promise.resolve();
  });
  expect(clientMock.runsList).not.toHaveBeenCalled();
  window.sessionStorage.clear();
});

test("skips discovery for mock clients", async () => {
  clientMock.runsList.mockClear();

  renderRejoin({ isMock: true });

  await act(async () => {
    await Promise.resolve();
  });
  expect(clientMock.runsList).not.toHaveBeenCalled();
});
