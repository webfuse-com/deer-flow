// [argus patch #103] Open tabs learn about runs they did not start.
import { afterEach, beforeEach, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";

import {
  liveActivityInvalidations,
  useLiveThreadActivity,
} from "@/core/threads/live-activity";

type Listener = (event: { data?: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  onerror: (() => void) | null = null;
  listeners = new Map<string, Listener[]>();
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  emit(type: string, data?: unknown) {
    for (const fn of this.listeners.get(type) ?? []) {
      fn({ data: data === undefined ? undefined : JSON.stringify(data) });
    }
  }
  close() {
    this.closed = true;
  }
}

let visibility: DocumentVisibilityState = "visible";

beforeEach(() => {
  FakeEventSource.instances = [];
  visibility = "visible";
  rs.useFakeTimers();
  (globalThis as { EventSource?: unknown }).EventSource = FakeEventSource;
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => visibility,
  });
});

afterEach(() => {
  cleanup();
  rs.useRealTimers();
  rs.clearAllMocks();
});

function mount() {
  const client = new QueryClient();
  const invalidate = rs.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  renderHook(() => useLiveThreadActivity(), { wrapper });
  const keys = () => invalidate.mock.calls.map((c) => c[0]?.queryKey);
  return { invalidate, keys };
}

test("a finished run refreshes the thread's runs and its history; a new one only its runs", () => {
  expect(
    liveActivityInvalidations({ thread_id: "t1", run_id: "r", status: "pending" }),
  ).toEqual([["thread", "t1"]]);
  expect(
    liveActivityInvalidations({ thread_id: "t1", run_id: "r", status: "success" }),
  ).toEqual([
    ["thread", "t1"],
    ["thread-messages", "t1"],
  ]);
});

test("a run started elsewhere makes the tab refetch, so the existing rejoin can stream it", () => {
  const { keys } = mount();
  const source = FakeEventSource.instances[0]!;
  expect(source.url).toBe("/api/threads/activity");
  act(() => {
    source.emit("ready", {});
    source.emit("run", { thread_id: "t1", run_id: "r1", status: "running" });
  });
  expect(keys()).toContainEqual(["thread", "t1"]);
  act(() => {
    rs.advanceTimersByTime(2000);
  });
  expect(keys()).toContainEqual(["threads", "searchInfinite"]); // the sidebar, debounced
});

test("a server-started run leaves the sidebar to upstream's activity feed", () => {
  const { keys } = mount();
  const source = FakeEventSource.instances[0]!;
  act(() => {
    source.emit("ready", {});
    source.emit("run", {
      thread_id: "t2",
      run_id: "r2",
      status: "running",
      origin_kind: "schedule",
    });
  });
  expect(keys()).toContainEqual(["thread", "t2"]); // still streams into an open thread
  act(() => {
    rs.advanceTimersByTime(2000);
  });
  expect(keys()).not.toContainEqual(["threads", "searchInfinite"]);
});

test("a hidden tab holds no stream and resyncs when shown again", () => {
  const { keys } = mount();
  const first = FakeEventSource.instances[0]!;
  act(() => first.emit("ready", {}));
  visibility = "hidden";
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(first.closed).toBe(true);
  visibility = "visible";
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const second = FakeEventSource.instances[1]!;
  act(() => second.emit("ready", {}));
  expect(keys()).toContainEqual(["thread"]); // everything it shows may have moved
});

test("a gateway without the stream is retried with a growing back-off, not every 3 s", () => {
  mount();
  act(() => FakeEventSource.instances[0]!.onerror?.());
  expect(FakeEventSource.instances).toHaveLength(1);
  act(() => {
    rs.advanceTimersByTime(2000);
  });
  expect(FakeEventSource.instances).toHaveLength(2);
  act(() => FakeEventSource.instances[1]!.onerror?.());
  act(() => {
    rs.advanceTimersByTime(3000);
  });
  expect(FakeEventSource.instances).toHaveLength(2); // next try only after 4 s
  act(() => {
    rs.advanceTimersByTime(1000);
  });
  expect(FakeEventSource.instances).toHaveLength(3);
});
