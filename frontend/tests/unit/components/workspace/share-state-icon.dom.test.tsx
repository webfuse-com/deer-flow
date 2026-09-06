import { afterEach, describe, expect, it } from "@rstest/core";
import { cleanup, render } from "@testing-library/react";
import { LockIcon, UsersIcon } from "lucide-react";

import {
  ShareStateIcon,
  shareStateGlyph,
} from "@/components/workspace/share-state-icon";

afterEach(cleanup);

describe("ShareStateIcon (DOM)", () => {
  it("draws a lock while private and people once shared", () => {
    const { container, rerender } = render(<ShareStateIcon shared={false} />);
    const privateGlyph = container.querySelector("svg");
    expect(privateGlyph).not.toBeNull();
    expect(privateGlyph?.getAttribute("class")).toContain("lucide-lock");
    expect(privateGlyph?.getAttribute("class")).toContain("size-4");
    expect(privateGlyph?.getAttribute("data-shared")).toBe("false");
    expect(privateGlyph?.getAttribute("aria-hidden")).toBe("true");

    rerender(<ShareStateIcon shared={true} className="size-5" />);
    const sharedGlyph = container.querySelector("svg");
    expect(sharedGlyph?.getAttribute("class")).toContain("lucide-users");
    expect(sharedGlyph?.getAttribute("class")).not.toContain("lucide-lock");
    expect(sharedGlyph?.getAttribute("class")).toContain("size-5");
    expect(sharedGlyph?.getAttribute("data-shared")).toBe("true");
  });

  it("exposes the same two glyphs for callers that take an icon prop", () => {
    expect(shareStateGlyph(false)).toBe(LockIcon);
    expect(shareStateGlyph(true)).toBe(UsersIcon);
  });
});
