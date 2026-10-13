import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { useSyncExternalStore } from "react";

import {
  DEFAULT_LOCAL_SETTINGS,
  safeLocalStorage,
} from "@/core/settings/local";
import { getBaseSettingsSnapshot, subscribe } from "@/core/settings/store";
import { UserPreferencesBoundary } from "@/core/settings/user-preferences-boundary";

// [argus patch #141] Cross-tab preferences sync must not ping-pong.
const mocks = rs.hoisted(() => ({
  user: { id: "alice" },
  fetch: rs.fn(),
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({ user: mocks.user }),
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "" }));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));
rs.mock("@/core/api/fetcher", () => ({ fetch: mocks.fetch }));

const KEY = "deerflow.preferences.alice";
// The Gateway's reply (atlas-salome, 2026-10-13: 110 bytes, every request).
const ACCOUNT = {
  notification_enabled: null,
  model_name: "local-qwen",
  mode: null,
  reasoning_effort: "high",
  locale: "en-US",
};
// The same reply as stored by a tab whose bundle predates the `locale`
// preference: it parses the reply into four fields, so its cache never
// equals the five-field cache of a newer tab.
const OLDER_TAB_CACHE = JSON.stringify({
  notification_enabled: null,
  model_name: "local-qwen",
  mode: null,
  reasoning_effort: "high",
});

function Consumer() {
  const settings = useSyncExternalStore(
    subscribe,
    getBaseSettingsSnapshot,
    () => DEFAULT_LOCAL_SETTINGS,
  );
  return <output>{settings.context.model_name ?? "default"}</output>;
}
function Workspace() {
  return (
    <UserPreferencesBoundary>
      <Consumer />
    </UserPreferencesBoundary>
  );
}

/**
 * Simulate an older sibling tab: whenever this tab writes the shared cache,
 * the older tab refetches the same reply and stores its own serialization,
 * and the browser delivers that write to this tab as a `storage` event.
 */
function olderSiblingTab(maxEchoes: number) {
  // jsdom's Storage is a proxy whose writes skip Storage.prototype, so watch
  // the app's own storage facade instead.
  const setItem = safeLocalStorage.setItem.bind(safeLocalStorage);
  let echoes = 0;
  const spy = rs
    .spyOn(safeLocalStorage, "setItem")
    .mockImplementation((key: string, value: string) => {
      const written = setItem(key, value);
      if (key !== KEY || value === OLDER_TAB_CACHE || echoes >= maxEchoes)
        return written;
      echoes += 1;
      setTimeout(() => {
        window.localStorage.setItem(KEY, OLDER_TAB_CACHE);
        window.dispatchEvent(
          new StorageEvent("storage", {
            key: KEY,
            oldValue: value,
            newValue: OLDER_TAB_CACHE,
            storageArea: window.localStorage,
          }),
        );
      }, 0);
      return written;
    });
  return { echoes: () => echoes, restore: () => spy.mockRestore() };
}

async function settle(ms: number) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  mocks.user = { id: "alice" };
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(async () => Response.json(ACCOUNT));
});
afterEach(() => {
  cleanup();
  rs.useRealTimers();
});

describe("cross-tab preferences sync", () => {
  it("does not refetch forever next to a tab that serializes the same reply differently", async () => {
    localStorage.setItem(KEY, OLDER_TAB_CACHE);
    const sibling = olderSiblingTab(25);
    render(<Workspace />);
    await screen.findByText("local-qwen");
    await settle(300);
    // One read at start. The older tab's rewrite carries no field that
    // disagrees with this tab, so it must not trigger another request.
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    expect(sibling.echoes()).toBe(1);
    sibling.restore();
  });

  it("still refetches when another tab stored a different value", async () => {
    render(<Workspace />);
    await screen.findByText("local-qwen");
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    const changed = { ...ACCOUNT, model_name: "glm-5.3" };
    mocks.fetch.mockImplementation(async () => Response.json(changed));
    act(() => {
      localStorage.setItem(KEY, JSON.stringify(changed));
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: KEY,
          newValue: JSON.stringify(changed),
          storageArea: localStorage,
        }),
      );
    });
    await screen.findByText("glm-5.3");
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
  });

  it("throttles refetches caused by a burst of storage events", async () => {
    render(<Workspace />);
    await screen.findByText("local-qwen");
    rs.useFakeTimers();
    for (let i = 0; i < 20; i++) {
      const value = JSON.stringify({ ...ACCOUNT, model_name: `m-${i}` });
      act(() => {
        window.dispatchEvent(
          new StorageEvent("storage", {
            key: KEY,
            newValue: value,
            storageArea: localStorage,
          }),
        );
      });
      await act(async () => {
        await rs.advanceTimersByTimeAsync(50);
      });
    }
    // 1 s of events: the start read plus at most one storage refetch.
    expect(mocks.fetch.mock.calls.length).toBeLessThanOrEqual(2);
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    // The trailing refetch still picks up the last write.
    expect(mocks.fetch.mock.calls.length).toBeLessThanOrEqual(3);
    expect(mocks.fetch.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
