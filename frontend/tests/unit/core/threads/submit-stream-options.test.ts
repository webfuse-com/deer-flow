import { expect, test } from "@rstest/core";

import { WEB_THREAD_SUBMIT_STREAM_OPTIONS } from "@/core/threads/submit-stream-options";

test("web submits keep the run alive when the SSE client leaves", () => {
  expect(WEB_THREAD_SUBMIT_STREAM_OPTIONS).toEqual({
    streamResumable: true,
    onDisconnect: "continue",
  });
});
