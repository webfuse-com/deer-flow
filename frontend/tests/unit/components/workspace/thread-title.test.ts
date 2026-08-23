import { expect, test } from "@rstest/core";

import { formatThreadDocumentTitle } from "@/components/workspace/thread-title";

test("running thread titles use the brain marker", () => {
  const title = formatThreadDocumentTitle({
    appName: "DeerFlow",
    isLoading: true,
    isThreadLoading: false,
    title: "Investigate input polish",
  });

  expect(title).toBe("🧠 [Running] Investigate input polish - DeerFlow");
  expect(title).not.toContain("⏳");
});
