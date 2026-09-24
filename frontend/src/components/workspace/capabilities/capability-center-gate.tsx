"use client";

// [argus patch #95] Keep a deep link from opening a Capability Center the
// deployment turned off: its saves would be overwritten by the next deploy.

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { useCapabilityCenterEnabled } from "@/core/features/hooks";

export function CapabilityCenterGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { enabled, isLoading } = useCapabilityCenterEnabled();

  useEffect(() => {
    if (!isLoading && !enabled) {
      router.replace("/workspace/chats");
    }
  }, [enabled, isLoading, router]);

  return enabled ? children : null;
}
