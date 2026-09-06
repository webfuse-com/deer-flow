import { afterEach, describe, expect, it, rs } from "@rstest/core";

import {
  fetchFileShareStatus,
  fetchSharedThread,
  fetchThreadShareStatus,
  internalizeFile,
  internalizeThread,
  previewThreadShare,
  readSharingResponse,
  refreshThreadShare,
  revokeFileShare,
  revokeThreadShare,
  SharingRequestError,
} from "@/core/sharing/api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function lastCall(fetchMock: ReturnType<typeof rs.spyOn>) {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit?];
  return { url, init };
}

async function captureError(
  promise: Promise<unknown>,
): Promise<SharingRequestError> {
  try {
    await promise;
  } catch (error) {
    return error as SharingRequestError;
  }
  throw new Error("expected the call to reject");
}

afterEach(() => {
  rs.restoreAllMocks();
});

describe("sharing api client", () => {
  it("reads owner status from /api/shared-threads/mine", async () => {
    const fetchMock = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () =>
        jsonResponse({ shared: false, share: null }),
      );
    await fetchThreadShareStatus("t 1");
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe("/api/shared-threads/mine/t%201");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.credentials).toBe("include");
  });

  it("previews with the tool-bodies flag", async () => {
    const fetchMock = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ counts: {} }));
    await previewThreadShare("t1");
    expect(lastCall(fetchMock).url).toBe(
      "/api/shared-threads/mine/t1/preview?include_tool_bodies=0",
    );
    await previewThreadShare("t1", { includeToolBodies: true });
    expect(lastCall(fetchMock).url).toBe(
      "/api/shared-threads/mine/t1/preview?include_tool_bodies=1",
    );
  });

  it("internalizes with a JSON body and revokes with DELETE", async () => {
    const fetchMock = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ shared: true }));
    await internalizeThread("t1", { includeToolBodies: true });
    let call = lastCall(fetchMock);
    expect(call.url).toBe("/api/shared-threads/mine/t1");
    expect(call.init?.method).toBe("POST");
    expect(JSON.parse(call.init?.body as string)).toEqual({
      include_tool_bodies: true,
    });

    await refreshThreadShare("t1");
    call = lastCall(fetchMock);
    expect(call.url).toBe("/api/shared-threads/mine/t1/refresh");
    expect(call.init?.method).toBe("POST");

    await revokeThreadShare("t1");
    call = lastCall(fetchMock);
    expect(call.url).toBe("/api/shared-threads/mine/t1");
    expect(call.init?.method).toBe("DELETE");
  });

  it("reads a colleague's snapshot from the stack-scoped route", async () => {
    const fetchMock = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () =>
        jsonResponse({ snapshot: { messages: [] } }),
      );
    await fetchSharedThread("atlas-nicholas", "tid");
    expect(lastCall(fetchMock).url).toBe(
      "/api/shared-threads/atlas-nicholas/tid",
    );
  });

  it("addresses file shares by thread and path", async () => {
    const fetchMock = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ shared: false }));
    await fetchFileShareStatus("t1", "/mnt/user-data/outputs/a b.md");
    expect(lastCall(fetchMock).url).toBe(
      "/api/shared-files/mine/t1?path=%2Fmnt%2Fuser-data%2Foutputs%2Fa%20b.md",
    );

    await internalizeFile("t1", "/mnt/user-data/outputs/a.md", "Report");
    let call = lastCall(fetchMock);
    expect(call.url).toBe("/api/shared-files/mine/t1");
    expect(call.init?.method).toBe("POST");
    expect(JSON.parse(call.init?.body as string)).toEqual({
      path: "/mnt/user-data/outputs/a.md",
      title: "Report",
    });

    await revokeFileShare("t1", "/mnt/user-data/outputs/a.md");
    call = lastCall(fetchMock);
    expect(call.init?.method).toBe("DELETE");
    expect(call.url).toContain("/api/shared-files/mine/t1?path=");
  });

  it("maps JSON errors to SharingRequestError with the Agora's code", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "not shared", code: "not_shared" }, 404),
    );
    const error = await captureError(fetchSharedThread("atlas-x", "tid"));
    expect(error).toBeInstanceOf(SharingRequestError);
    expect(error.status).toBe(404);
    expect(error.code).toBe("not_shared");
    expect(error.message).toBe("not shared");
    expect(error.isSessionExpired).toBe(false);
  });

  it("treats an HTML or redirected answer as an expired session", async () => {
    const html = new Response("<html>login</html>", {
      status: 200,
      headers: { "Content-Type": "text/html" },
    });
    const error = await captureError(readSharingResponse(html));
    expect(error).toBeInstanceOf(SharingRequestError);
    expect(error.code).toBe("session_expired");
    expect(error.isSessionExpired).toBe(true);
  });

  it("treats a failed cross-origin fetch as an expired session", async () => {
    rs.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const error = await captureError(fetchThreadShareStatus("t1"));
    expect(error).toBeInstanceOf(SharingRequestError);
    expect(error.code).toBe("session_expired");
  });
});
