import { describe, expect, it } from "@rstest/core";

// [argus patch #167]
import { commandHref, loadExtensionCommands } from "@/core/extensions/commands";
import type { CommandProvider } from "@/core/extensions/contracts";
import type { LoadedContribution } from "@/core/extensions/registry";

const entry = (
  namespace: string,
  items: (...args: never[]) => unknown,
  provider: Partial<CommandProvider> = {},
): LoadedContribution => ({
  namespace,
  module: namespace,
  entry: null,
  title: namespace,
  description: "",
  settings: { enabled: true },
  extension: {
    apiVersion: 1,
    module: namespace,
    commandProviders: [
      { id: "apps", label: "Apps", items: items as never, ...provider },
    ],
  },
});
const context = () => ({
  locale: "en-US",
  signal: new AbortController().signal,
});

describe("command hrefs", () => {
  it("keeps https: URLs and /workspace/ paths only", () => {
    expect(commandHref("https://wiki.surfly.com")).toEqual({
      kind: "external",
      href: "https://wiki.surfly.com/",
    });
    expect(commandHref("/workspace/projects/abc")).toEqual({
      kind: "internal",
      href: "/workspace/projects/abc",
    });
    for (const bad of [
      "javascript:alert(1)",
      "http://example.com",
      "//evil.example/x",
      "/api/plugins",
      "https://user:pw@example.com",
      "/workspace/\\evil",
      "",
      42,
    ])
      expect(commandHref(bad)).toBeNull();
  });
});

describe("extension commands", () => {
  it("groups valid entries and drops invalid, duplicate and unsafe ones", async () => {
    const result = await loadExtensionCommands(
      [
        entry(
          "argus",
          async () => [
            {
              id: "a",
              label: "Semantic Selectors",
              description: "semantic-selectors · Nicholas",
              keywords: ["semantic-selectors", 7, "x".repeat(200)],
              href: "https://apps-nicholas.acro.surfly.com/semantic-selectors/",
            },
            { id: "a", label: "Duplicate", href: "https://example.com" },
            { id: "b", label: "Script", href: "javascript:alert(1)" },
            { id: {}, label: "Invalid", href: "https://example.com" },
          ],
          { icon: "app-window" },
        ),
      ],
      context(),
    );
    expect(result.failed).toBe(false);
    expect(result.groups).toEqual([
      {
        key: "argus/apps",
        label: "Apps",
        icon: "app-window",
        position: "end",
        items: [
          {
            id: "a",
            label: "Semantic Selectors",
            description: "semantic-selectors · Nicholas",
            keywords: ["semantic-selectors"],
            href: "https://apps-nicholas.acro.surfly.com/semantic-selectors/",
            kind: "external",
          },
        ],
      },
    ]);
  });

  it("isolates a failing provider and skips disabled extensions", async () => {
    const result = await loadExtensionCommands(
      [
        entry("broken", async () => {
          throw Error("offline");
        }),
        entry("team", async () => [
          { id: "w", label: "Wiki", href: "https://wiki.surfly.com" },
        ]),
        {
          ...entry("disabled", async () => [
            { id: "x", label: "Hidden", href: "https://example.com" },
          ]),
          settings: { enabled: false },
        },
      ],
      context(),
    );
    expect(result.groups.map((group) => group.key)).toEqual(["team/apps"]);
    expect(result.failed).toBe(true);
  });

  it("settles when a provider never answers", async () => {
    const abort = new AbortController();
    const pending = loadExtensionCommands(
      [entry("slow", () => new Promise(() => undefined))],
      { locale: "en-US", signal: abort.signal },
    );
    abort.abort();
    await expect(pending).rejects.toThrow();
  });
});
