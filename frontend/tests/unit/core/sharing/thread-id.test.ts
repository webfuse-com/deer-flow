import { describe, expect, it } from "@rstest/core";

import {
  isSharedThreadId,
  isValidStack,
  isValidThreadId,
  parseSharedThreadId,
  toSharedThreadId,
} from "@/core/sharing/thread-id";

describe("shared thread ids", () => {
  it("round-trips a stack and thread id", () => {
    const id = toSharedThreadId(
      "atlas-nicholas",
      "aa5151dc-0000-4000-8000-000000000001",
    );
    expect(id).toBe(
      "shared:atlas-nicholas:aa5151dc-0000-4000-8000-000000000001",
    );
    expect(parseSharedThreadId(id)).toEqual({
      stack: "atlas-nicholas",
      threadId: "aa5151dc-0000-4000-8000-000000000001",
    });
    expect(isSharedThreadId(id)).toBe(true);
  });

  it("does not mistake a plain thread id for a shared one", () => {
    expect(parseSharedThreadId("abc")).toBeNull();
    expect(
      parseSharedThreadId("aa5151dc-0000-4000-8000-000000000001"),
    ).toBeNull();
    expect(isSharedThreadId("shared:")).toBe(false);
    expect(isSharedThreadId("shared::tid")).toBe(false);
    expect(isSharedThreadId("shared:atlas-x")).toBe(false);
  });

  it("validates the same shapes as the Agora", () => {
    expect(isValidStack("atlas-nicholas")).toBe(true);
    expect(isValidStack("Atlas")).toBe(false);
    expect(isValidStack("a")).toBe(false);
    expect(isValidStack("atlas/nicholas")).toBe(false);
    expect(isValidThreadId("aa5151dc-0000-4000-8000-000000000001")).toBe(true);
    expect(isValidThreadId("thread id")).toBe(false);
    expect(isValidThreadId("../etc")).toBe(false);
    expect(isValidThreadId("")).toBe(false);
  });

  it("rejects shared ids whose parts fail validation", () => {
    expect(parseSharedThreadId("shared:Atlas:tid")).toBeNull();
    expect(parseSharedThreadId("shared:atlas-x:has space")).toBeNull();
  });
});
