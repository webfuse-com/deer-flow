import { describe, expect, it } from "@rstest/core";

import { getMessageGroups } from "@/core/messages/utils";
import type { SharedThreadPayload } from "@/core/sharing/api";
import {
  normalizeSnapshotMessages,
  snapshotToStream,
} from "@/core/sharing/snapshot";

import fixture from "../../fixtures/shared-thread-snapshot.json";

const payload = fixture as unknown as SharedThreadPayload;

describe("normalizeSnapshotMessages", () => {
  it("keeps human, ai and tool rows and drops everything else", () => {
    const messages = normalizeSnapshotMessages(payload.snapshot.messages);
    expect(messages.map((m) => m.type)).toEqual(["human", "ai", "tool", "ai"]);
  });

  it("preserves the ids the renderer pairs on", () => {
    const messages = normalizeSnapshotMessages(payload.snapshot.messages);
    const [, call, result] = messages;
    expect(call!.id).toBe("a1");
    expect(
      (call as { tool_calls: { id: string; name: string }[] }).tool_calls,
    ).toEqual([
      {
        id: "c1",
        name: "bash",
        args: { command: "python3 seats.py --quarter Q2" },
        type: "tool_call",
      },
    ]);
    expect((result as { tool_call_id: string }).tool_call_id).toBe("c1");
    expect(result!.name).toBe("bash");
    expect(result!.additional_kwargs).toEqual({ omitted_chars: 1834 });
  });

  it("synthesizes ids and tolerates junk", () => {
    const messages = normalizeSnapshotMessages([
      { type: "human", content: "hi" },
      "not a message",
      {
        type: "ai",
        content: [{ type: "text", text: "yo" }, 42],
        tool_calls: "nope",
      },
      { type: "tool", content: null },
    ]);
    expect(messages.map((m) => m.id)).toEqual(["snap-0", "snap-2", "snap-3"]);
    expect(messages[1]!.content).toEqual([{ type: "text", text: "yo" }]);
    expect((messages[1] as { tool_calls: unknown[] }).tool_calls).toEqual([]);
    expect(messages[2]!.content).toBe("");
    expect(normalizeSnapshotMessages("garbage")).toEqual([]);
  });
});

describe("snapshotToStream", () => {
  it("builds a settled stream the message list can render", () => {
    const stream = snapshotToStream(payload);
    expect(stream.isLoading).toBe(false);
    expect(stream.isThreadLoading).toBe(false);
    expect(stream.error).toBeUndefined();
    expect(stream.messages).toHaveLength(4);
    expect(stream.values.title).toBe("Q2 seat forecast");
    expect(stream.values.artifacts).toEqual([
      "/mnt/user-data/outputs/report.md",
    ]);
    expect(stream.getMessagesMetadata(stream.messages[0]!)).toBeUndefined();
  });

  it("groups the fixture the way the live feed would", () => {
    const stream = snapshotToStream(payload);
    const groups = getMessageGroups(stream.messages);
    expect(groups.map((g) => g.type)).toEqual([
      "human",
      "assistant:processing",
      "assistant",
    ]);
    const processing = groups[1]!;
    expect(processing.type).toBe("assistant:processing");
    if (processing.type === "assistant:processing") {
      expect(processing.messages.map((m) => m.type)).toEqual(["ai", "tool"]);
    }
  });
});
