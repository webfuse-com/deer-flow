import { describe, expect, it } from "@rstest/core";

import { filesSourceOf, orderedTabs, repoFileOf } from "@/core/projects/files";

const REPO = "https://github.com/acme/atlas-x";
const project = (presentation: Record<string, unknown>) => ({ presentation });
const source = filesSourceOf(
  project({
    files: {
      folder_url: `${REPO}/tree/main/projects/alpha`,
      file_url: `${REPO}/blob/main/projects/alpha/{path}`,
      edit_url: "javascript:alert(1)/{path}",
      writable: "refs/",
      editable_suffixes: [".md", "md", 3],
    },
  }),
);

describe("[argus patch #118] project files in a repository", () => {
  it("reads only https links and dotted suffixes", () => {
    expect(source).toEqual({
      folder_url: `${REPO}/tree/main/projects/alpha`,
      file_url: `${REPO}/blob/main/projects/alpha/{path}`,
      edit_url: undefined,
      writable: "refs/",
      editable_suffixes: [".md"],
    });
    expect(filesSourceOf(project({}))).toBeNull();
    expect(filesSourceOf(project({ files: [] }))).toBeNull();
  });

  it("knows where a mirrored file lives and what the app may do with it", () => {
    expect(
      repoFileOf(
        { source_kind: "mirror", source_name: "refs/Notes.MD" },
        source,
      ),
    ).toEqual({
      path: "refs/Notes.MD",
      url: `${REPO}/blob/main/projects/alpha/refs/Notes.MD`,
      editUrl: null,
      writable: true,
      editable: true,
    });
    const chapter = repoFileOf(
      { source_kind: "edit", source_name: "content/a b.md" },
      source,
    )!;
    expect(chapter.writable).toBe(false);
    expect(chapter.editable).toBe(false);
    expect(chapter.url).toBe(
      `${REPO}/blob/main/projects/alpha/content/a%20b.md`,
    );
    expect(
      repoFileOf({ source_kind: "mirror", source_name: "refs/x.pdf" }, source)!
        .editable,
    ).toBe(false);
    expect(
      repoFileOf({ source_kind: "upload", source_name: "x.md" }, source),
    ).toBeNull();
    expect(
      repoFileOf({ source_kind: "mirror", source_name: "../x.md" }, source),
    ).toBeNull();
    expect(
      repoFileOf({ source_kind: "mirror", source_name: "refs/x.md" }, null),
    ).toBeNull();
  });

  it("orders tabs by the presentation, then the rest", () => {
    const defaults = [
      "chats",
      "documents",
      "instructions",
      "settings",
      "plugin:argus:overview",
    ];
    expect(
      orderedTabs(
        defaults,
        project({ tabs: ["plugin:argus:overview", "chats", "bogus", "chats"] }),
      ),
    ).toEqual([
      "plugin:argus:overview",
      "chats",
      "documents",
      "instructions",
      "settings",
    ]);
    expect(orderedTabs(defaults, project({}))).toEqual(defaults);
  });
});
