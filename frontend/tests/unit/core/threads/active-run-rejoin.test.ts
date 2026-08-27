import type { Run } from "@langchain/langgraph-sdk";
import { describe, expect, it } from "@rstest/core";

import { pickActiveRun } from "@/core/threads/active-run-rejoin";

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

describe("pickActiveRun", () => {
  it("returns undefined for an empty or undefined list", () => {
    expect(pickActiveRun(undefined)).toBeUndefined();
    expect(pickActiveRun([])).toBeUndefined();
  });

  it("returns undefined when every run is terminal", () => {
    expect(
      pickActiveRun([run("success"), run("error"), run("interrupted")]),
    ).toBeUndefined();
  });

  it("picks the first (newest) active run", () => {
    const active = run("running", "active");
    expect(
      pickActiveRun([run("success"), active, run("pending", "pending")]),
    ).toBe(active);
  });

  it("picks a pending run", () => {
    const pending = run("pending");
    expect(pickActiveRun([pending])).toBe(pending);
  });

  it("skips terminal runs to find an older active one", () => {
    const active = run("running", "older-active");
    expect(pickActiveRun([run("success", "newest"), active])).toBe(active);
  });
});
