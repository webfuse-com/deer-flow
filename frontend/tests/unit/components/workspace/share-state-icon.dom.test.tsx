import { afterEach, describe, expect, it } from "@rstest/core";
import { cleanup, render } from "@testing-library/react";

import {
  ShareStateIcon,
  shareStateGlyph,
} from "@/components/workspace/share-state-icon";

afterEach(cleanup);

describe("ShareStateIcon (DOM)", () => {
  it("renders one svg glyph per state and marks the shared state", () => {
    const { container, rerender } = render(<ShareStateIcon shared={false} />);
    const privateGlyph = container.querySelector("svg");
    expect(privateGlyph).not.toBeNull();
    expect(privateGlyph?.getAttribute("data-shared")).toBe("false");
    expect(privateGlyph?.getAttribute("aria-hidden")).toBe("true");

    rerender(<ShareStateIcon shared={true} className="size-4" />);
    const sharedGlyph = container.querySelector("svg");
    expect(sharedGlyph?.getAttribute("data-shared")).toBe("true");
    expect(sharedGlyph?.getAttribute("class")).toContain("size-4");
  });

  it("exposes the glyph component for callers that take an icon prop", () => {
    expect(typeof shareStateGlyph(false)).not.toBe("undefined");
    expect(typeof shareStateGlyph(true)).not.toBe("undefined");
  });
});
