// [argus patch #110] Agents are created with New agent in the gallery;
// the old wizard URL redirects there (upstream's wizard and its save test,
// new-agent-save.dom.test.tsx, are not carried).
import { expect, rs, test } from "@rstest/core";

const redirect = rs.hoisted(() => rs.fn());
rs.mock("next/navigation", () => ({ redirect }));

import NewAgentPage from "@/app/workspace/agents/new/page";

test("the old creation wizard URL redirects to the agents gallery", () => {
  NewAgentPage();
  expect(redirect).toHaveBeenCalledWith("/workspace/agents");
});
