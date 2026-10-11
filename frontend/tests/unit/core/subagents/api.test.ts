import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "" }));

import { fetch as fetcher } from "@/core/api/fetcher";
import {
  createManagedSubagent,
  deleteManagedSubagent,
  listSubagents,
  updateManagedSubagent,
} from "@/core/subagents/api";

const mockedFetch = rs.mocked(fetcher);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("managed subagent API", () => {
  test("lists the mixed-source catalog", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        subagents: [
          {
            name: "general-purpose",
            description: "General worker",
            source: "builtin",
            enabled: true,
          },
        ],
      }),
    );
    await expect(listSubagents()).resolves.toHaveLength(1);
  });

  test("creates and updates managed definitions through admin endpoints", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(201, {
          name: "planner",
          description: "Plans",
          source: "managed",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          name: "planner",
          description: "Plans",
          source: "managed",
          enabled: false,
        }),
      );

    await createManagedSubagent({
      name: "planner",
      description: "Plans",
      system_prompt: "You plan.",
    });
    await updateManagedSubagent("planner", { enabled: false });

    expect(mockedFetch.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(mockedFetch.mock.calls[1]?.[1]?.method).toBe("PUT");
    expect(JSON.parse(mockedFetch.mock.calls[1]?.[1]?.body as string)).toEqual({
      enabled: false,
    });
  });

  test("surfaces backend conflict details", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, { detail: "Subagent name is reserved" }),
    );
    await expect(
      createManagedSubagent({
        name: "general-purpose",
        description: "Duplicate",
        system_prompt: "Duplicate",
      }),
    ).rejects.toThrow("Subagent name is reserved");
  });

  test("formats FastAPI validation detail arrays", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(422, {
        detail: [{ loc: ["body", "name"], msg: "String should match pattern" }],
      }),
    );
    await expect(
      createManagedSubagent({
        name: "invalid_name",
        description: "Invalid",
        system_prompt: "Invalid",
      }),
    ).rejects.toThrow("String should match pattern");
  });
});

describe("[argus patch #111] thinking and store refusals", () => {
  test("sends thinking_enabled on update", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        name: "planner",
        description: "Plans",
        source: "managed",
        thinking_enabled: true,
      }),
    );
    const updated = await updateManagedSubagent("planner", {
      thinking_enabled: true,
    });
    expect(updated.thinking_enabled).toBe(true);
    expect(JSON.parse(mockedFetch.mock.calls[0]?.[1]?.body as string)).toEqual({
      thinking_enabled: true,
    });
  });

  test("an update refused by the store throws the server's text", async () => {
    const detail =
      "Changed on GitHub since this page loaded (abc1234). Reload and try again.";
    mockedFetch.mockResolvedValueOnce(jsonResponse(409, { detail }));
    await expect(
      updateManagedSubagent("planner", { description: "Plans more" }),
    ).rejects.toThrow(detail);
  });

  test("a delete refused by the store throws the server's text", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(502, { detail: "Not saved to GitHub: rate limited" }),
    );
    await expect(deleteManagedSubagent("planner")).rejects.toThrow(
      "Not saved to GitHub: rate limited",
    );
  });
});
