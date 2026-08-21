import { expect, test } from "@rstest/core";

import { sanitizeRunStreamOptions } from "@/core/api/stream-mode";

test("drops unsupported modes when at least one requested mode is supported", () => {
  expect(
    sanitizeRunStreamOptions({
      streamMode: ["values", "events", "tools"],
    }),
  ).toEqual({ streamMode: ["values"] });
});

test("rejects payloads when every requested stream mode is unsupported", () => {
  expect(() =>
    sanitizeRunStreamOptions({
      streamMode: ["events", "tools"],
    }),
  ).toThrow("Unsupported LangGraph stream mode(s): events, tools");

  expect(() =>
    sanitizeRunStreamOptions({
      streamMode: "tools",
    }),
  ).toThrow("Unsupported LangGraph stream mode(s): tools");
});

test("rejects messages because the Gateway only supports messages-tuple framing", () => {
  expect(() =>
    sanitizeRunStreamOptions({
      streamMode: "messages",
    }),
  ).toThrow("Unsupported LangGraph stream mode(s): messages");
});

test("keeps payloads without streamMode untouched", () => {
  const options = {
    streamSubgraphs: true,
  };

  expect(sanitizeRunStreamOptions(options)).toBe(options);
});

test("strips streamResumable before sending run options to the API", () => {
  const sanitized = sanitizeRunStreamOptions({
    streamResumable: true,
    streamSubgraphs: true,
  });

  expect(sanitized).toEqual({
    streamSubgraphs: true,
  });
});

test("sanitizes streamResumable while preserving valid stream modes", () => {
  const sanitized = sanitizeRunStreamOptions({
    streamResumable: true,
    streamMode: ["values", "custom"],
  });

  expect(sanitized).toEqual({
    streamMode: ["values", "custom"],
  });
});
