"use client";

// [argus patch #95] Keep a deep link from opening a Capability Center the
// deployment turned off: its saves would be overwritten by the next deploy.

import { useRouter } from "next/navigation";
import { useEffect, useSyncExternalStore, type ReactNode } from "react";

import { useCapabilityCenterEnabled } from "@/core/features/hooks";

const subscribeNever = () => () => undefined;

export function CapabilityCenterGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { enabled, isLoading } = useCapabilityCenterEnabled();
  // The server never has the feature flag, so it renders nothing. Hydration
  // must render the same: the page sits in a Suspense boundary that can
  // hydrate after another component (the sidebar link) already fetched the
  // flag into the shared query cache, and rendering children then is a
  // hydration mismatch (React #418). Children appear on the first client
  // render after hydration.
  const hydrated = useSyncExternalStore(
    subscribeNever,
    () => true,
    () => false,
  );

  useEffect(() => {
    if (!isLoading && !enabled) {
      router.replace("/workspace/chats");
    }
  }, [enabled, isLoading, router]);

  return hydrated && enabled ? children : null;
}
