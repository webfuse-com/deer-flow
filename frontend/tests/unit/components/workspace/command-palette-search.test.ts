import { describe, expect, it } from "@rstest/core";

// [argus patch #167]
import {
  commandScore,
  rankCommandGroups,
  type SearchableItem,
} from "@/components/workspace/command-palette-search";

// Real names from atlas-nicholas and the Agora catalog on 2026-10-27.
type Group = { key: string; items: SearchableItem[] };
const app = (label: string, slug: string, owner: string): SearchableItem => ({
  label,
  description: `${slug} · ${owner}`,
  keywords: [slug],
});
const apps = [
  app("Customer Health Score Dashboard", "customer-health-score", "Marko"),
  app("Deploy Calendar", "deploy-calendar", "Tony"),
  app("Disco Ball — Three.js Demo", "disco-ball-threejs", "Nicholas"),
  app("2026 Pipeline & Forecast Dashboard", "forecast-dashboard", "Thomas"),
  app("NICE Data Hub — Royalty Monthly Review", "nice-data-hub", "Pedro"),
  app(
    "STR-2026-0814: Resilient Web Element Relocalization",
    "semantic-selectors",
    "Nicholas",
  ),
];
const projects = [
  "Malauzai Flow Automation",
  "Webfuse Augmentation Blueprint",
  "Redteam Loop",
  "Semantic Selectors",
  "Audit Enricher Ext",
].map((label): SearchableItem => ({ label }));
const tools: SearchableItem[] = [
  {
    label: "Drata",
    description: "Compliance and audits",
    keywords: ["compliance", "soc2", "audit"],
  },
  {
    label: "Chargebee",
    description: "Billing and subscriptions",
    keywords: ["billing", "invoices"],
  },
  {
    label: "Confluence",
    description: "Company wiki",
    keywords: ["wiki", "docs"],
  },
];
const groups: Group[] = [
  { key: "apps", items: apps },
  { key: "projects", items: projects },
  { key: "tools", items: tools },
];
const labels = (query: string) =>
  rankCommandGroups(groups, query).flatMap((group) =>
    group.items.map((item) => item.label),
  );

describe("command palette search", () => {
  it("puts an exact name first and leaves scattered letters out", () => {
    expect(labels("drata")).toEqual(["Drata"]);
    expect(rankCommandGroups(groups, "drata").map((g) => g.key)).toEqual([
      "tools",
    ]);
  });

  it("finds a project by name and an app by its slug", () => {
    expect(labels("sem")).toEqual([
      "Semantic Selectors",
      "STR-2026-0814: Resilient Web Element Relocalization",
    ]);
  });

  it("matches what a tool does, an owner, and every word of the query", () => {
    expect(labels("billing")).toEqual(["Chargebee"]);
    expect(labels("tony")).toEqual(["Deploy Calendar"]);
    expect(labels("dashboard forecast")).toEqual([
      "2026 Pipeline & Forecast Dashboard",
    ]);
    expect(labels("wiki")).toEqual(["Confluence"]);
    // inside a name counts; inside a keyword ("invoices") does not
    expect(labels("ice")).toEqual(["NICE Data Hub — Royalty Monthly Review"]);
  });

  it("keeps the given order with no query and drops empty groups", () => {
    const ranked = rankCommandGroups(
      [...groups, { key: "empty", items: [] }],
      "  ",
    );
    expect(ranked.map((group) => group.key)).toEqual([
      "apps",
      "projects",
      "tools",
    ]);
    expect(ranked[0]!.items).toEqual(apps);
  });

  it("scores a label prefix above a word inside it above a keyword", () => {
    expect(commandScore({ label: "Deploy Calendar" }, "dep")).toBeGreaterThan(
      commandScore({ label: "Release Deploy" }, "dep"),
    );
    expect(commandScore({ label: "Release Deploy" }, "dep")).toBeGreaterThan(
      commandScore({ label: "X", keywords: ["deploy"] }, "dep"),
    );
    expect(commandScore({ label: "X" }, "y")).toBe(0);
  });
});
