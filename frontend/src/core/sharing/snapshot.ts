/**
 * Turn an Agora thread snapshot into the stream shape `MessageList` renders.
 *
 * The snapshot carries LangChain-shaped message dicts (the same
 * `model_dump()` the gateway writes to `run_events`), so the only work here is
 * defensive validation and building a `BaseStream`-shaped object whose few
 * read members (`messages`, `values`, the loading flags,
 * `getMessagesMetadata`) are what the message components consult.
 */
import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";

import type { AgentThreadState } from "@/core/threads/types";

import type { SharedThreadPayload } from "./api";

const MESSAGE_TYPES = new Set(["human", "ai", "tool"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeContent(content: unknown): string | unknown[] {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content.filter(
      (block) => typeof block === "string" || isRecord(block),
    );
  }
  return "";
}

function normalizeToolCalls(raw: unknown): Record<string, unknown>[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  const out: Record<string, unknown>[] = [];
  for (const call of raw) {
    if (!isRecord(call) || typeof call.name !== "string") {
      continue;
    }
    out.push({
      id: typeof call.id === "string" ? call.id : undefined,
      name: call.name,
      args: isRecord(call.args) ? call.args : {},
      type: "tool_call",
    });
  }
  return out;
}

/**
 * Drop anything that is not a renderable human, ai or tool message, keep the
 * ids the message components pair on (message ids, tool call ids), and
 * synthesize an id when the snapshot has none.
 */
export function normalizeSnapshotMessages(raw: unknown): Message[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  const messages: Message[] = [];
  raw.forEach((item, index) => {
    if (!isRecord(item) || typeof item.type !== "string") {
      return;
    }
    if (!MESSAGE_TYPES.has(item.type)) {
      return;
    }
    const message: Record<string, unknown> = {
      type: item.type,
      id: typeof item.id === "string" && item.id ? item.id : `snap-${index}`,
      content: normalizeContent(item.content),
    };
    if (typeof item.name === "string") {
      message.name = item.name;
    }
    if (isRecord(item.additional_kwargs)) {
      message.additional_kwargs = item.additional_kwargs;
    }
    if (typeof item.status === "string") {
      message.status = item.status;
    }
    if (item.type === "ai") {
      message.tool_calls = normalizeToolCalls(item.tool_calls);
    }
    if (item.type === "tool" && typeof item.tool_call_id === "string") {
      message.tool_call_id = item.tool_call_id;
    }
    messages.push(message as unknown as Message);
  });
  return messages;
}

export function snapshotToStream(
  payload: SharedThreadPayload,
): BaseStream<AgentThreadState> {
  const messages = normalizeSnapshotMessages(payload.snapshot.messages);
  const artifacts = Array.isArray(payload.snapshot.values.artifacts)
    ? payload.snapshot.values.artifacts.filter(
        (path): path is string => typeof path === "string",
      )
    : [];
  const values: AgentThreadState = {
    title: payload.snapshot.values.title ?? payload.share.title,
    messages,
    artifacts,
    todos: [],
    goal: null,
  };
  const noop = async () => undefined;
  const stream = {
    messages,
    values,
    isLoading: false,
    isThreadLoading: false,
    error: undefined,
    interrupt: undefined,
    history: [],
    branch: "",
    assistantId: "shared",
    getMessagesMetadata: () => undefined,
    setBranch: () => undefined,
    submit: noop,
    stop: noop,
    joinStream: noop,
  };
  return stream as unknown as BaseStream<AgentThreadState>;
}
