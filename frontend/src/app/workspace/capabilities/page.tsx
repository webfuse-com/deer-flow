import { Suspense } from "react";

import { CapabilityCenter } from "@/components/workspace/capabilities/capability-center";
import { CapabilityCenterGate } from "@/components/workspace/capabilities/capability-center-gate";

export default function CapabilitiesPage() {
  return (
    <Suspense>
      <CapabilityCenterGate>
        <CapabilityCenter />
      </CapabilityCenterGate>
    </Suspense>
  );
}
