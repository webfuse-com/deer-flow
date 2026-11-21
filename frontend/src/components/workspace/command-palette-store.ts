"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

// [argus patch #167] One open state for the single command palette, so the
// sidebar's Apps entry and the shortcut open the same dialog (the same
// external-store shape as settings-dialog-store.ts).

type Listener = () => void;

const listeners = new Set<Listener>();
let open = false;

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function setCommandPaletteOpen(
  next: boolean | ((open: boolean) => boolean),
) {
  const value = typeof next === "function" ? next(open) : next;
  if (value === open) return;
  open = value;
  for (const listener of listeners) listener();
}

export function useCommandPaletteOpen(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => open,
    () => false,
  );
}

/** "⌘" on a Mac, "Ctrl+" elsewhere; "Ctrl+" until the client has mounted. */
export function useMetaKeyLabel(): string {
  const [isMac, setIsMac] = useState(false);
  useEffect(() => {
    setIsMac(navigator.userAgent.includes("Mac"));
  }, []);
  return isMac ? "⌘" : "Ctrl+";
}
