/**
 * Sharing API client (Argus patch #88).
 *
 * These calls are same-origin: the Caddy edge proxies `/api/shared-threads/*`
 * and `/api/shared-files/*` on every Atlas host to the Agora, which
 * authenticates the SSO identity and resolves the caller's own stack. The
 * gateway never sees them, so the shape is the Agora's: JSON errors carry
 * `{detail, code}`.
 *
 * An expired SSO session does not surface as a 401 here. The edge answers with
 * a redirect to the login page, which `fetch` follows into an HTML document.
 * Any redirected or non-JSON response is therefore reported as
 * `session_expired`, and the UI offers a reload.
 */
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

export interface ThreadShareInfo {
  title: string;
  include_tool_bodies: boolean;
  snapshot_at: string;
  snapshot_seq: number | null;
  snapshot_bytes: number;
  files_bytes: number;
  files_included: boolean;
  scrub_findings: number;
  revoked_at: string | null;
}

export interface ThreadShareStatus {
  thread_id: string;
  stack: string;
  shared: boolean;
  share: ThreadShareInfo | null;
  share_url: string;
  view_path: string;
}

export interface ThreadSharePreview {
  title: string;
  source: string;
  include_tool_bodies: boolean;
  counts: Record<string, number>;
  text_bytes: number;
  omitted_tool_chars: number;
  scrub: { count: number; kinds: Record<string, number> };
  files: {
    count: number;
    bytes: number;
    included: boolean;
    list: { path: string; bytes: number }[];
  };
}

export interface SharedThreadPayload {
  share: {
    stack: string;
    thread_id: string;
    title: string;
    owner_email: string;
    snapshot_at: string;
    include_tool_bodies: boolean;
    files_included: boolean;
    scrub_findings: number;
  };
  is_owner: boolean;
  share_url: string;
  snapshot: {
    schema_version: number;
    messages: unknown[];
    values: { title?: string; artifacts?: string[] };
    files: { path: string; bytes: number }[];
    omitted: { tool_bodies: number };
  };
}

export interface FileShareInfo {
  id: number;
  rel_path: string;
  title: string;
  bytes: number;
  content_type: string | null;
  scrub_findings: number;
  shared_at: string;
  revoked_at: string | null;
}

export interface FileShareStatus {
  shared: boolean;
  share: FileShareInfo | null;
  share_url: string | null;
}

export class SharingRequestError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "SharingRequestError";
    this.status = status;
    this.code = code;
  }

  get isSessionExpired(): boolean {
    return this.code === "session_expired";
  }
}

const SESSION_EXPIRED_MESSAGE =
  "Your session has expired. Reload the page to sign in again.";

function threadsBase(): string {
  return `${getBackendBaseURL()}/api/shared-threads`;
}

function filesBase(): string {
  return `${getBackendBaseURL()}/api/shared-files`;
}

async function request(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(url, init);
  } catch (error) {
    // A redirect into another origin (the SSO login page) fails the fetch
    // itself; the gateway-style 401 handling never gets a chance to run.
    if (error instanceof TypeError) {
      throw new SharingRequestError(
        SESSION_EXPIRED_MESSAGE,
        0,
        "session_expired",
      );
    }
    throw error;
  }
}

export async function readSharingResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") ?? "";
  if (response.redirected || !contentType.includes("application/json")) {
    throw new SharingRequestError(
      SESSION_EXPIRED_MESSAGE,
      response.status,
      "session_expired",
    );
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    let code = "error";
    try {
      const body = (await response.json()) as {
        detail?: unknown;
        code?: unknown;
      };
      if (typeof body.detail === "string" && body.detail) {
        detail = body.detail;
      }
      if (typeof body.code === "string" && body.code) {
        code = body.code;
      }
    } catch {
      // Keep the fallback message.
    }
    throw new SharingRequestError(detail, response.status, code);
  }
  return (await response.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

// ---------------------------------------------------------------- threads --

export async function fetchThreadShareStatus(
  threadId: string,
): Promise<ThreadShareStatus> {
  const response = await request(
    `${threadsBase()}/mine/${encodeURIComponent(threadId)}`,
  );
  return readSharingResponse<ThreadShareStatus>(response);
}

export async function previewThreadShare(
  threadId: string,
  { includeToolBodies = false }: { includeToolBodies?: boolean } = {},
): Promise<ThreadSharePreview> {
  const response = await request(
    `${threadsBase()}/mine/${encodeURIComponent(threadId)}/preview?include_tool_bodies=${includeToolBodies ? "1" : "0"}`,
  );
  return readSharingResponse<ThreadSharePreview>(response);
}

export async function internalizeThread(
  threadId: string,
  { includeToolBodies = false }: { includeToolBodies?: boolean } = {},
): Promise<ThreadShareStatus> {
  const response = await request(
    `${threadsBase()}/mine/${encodeURIComponent(threadId)}`,
    jsonInit("POST", { include_tool_bodies: includeToolBodies }),
  );
  return readSharingResponse<ThreadShareStatus>(response);
}

export async function refreshThreadShare(
  threadId: string,
  options: { includeToolBodies?: boolean } = {},
): Promise<ThreadShareStatus> {
  const body =
    options.includeToolBodies === undefined
      ? {}
      : { include_tool_bodies: options.includeToolBodies };
  const response = await request(
    `${threadsBase()}/mine/${encodeURIComponent(threadId)}/refresh`,
    jsonInit("POST", body),
  );
  return readSharingResponse<ThreadShareStatus>(response);
}

export async function revokeThreadShare(
  threadId: string,
): Promise<ThreadShareStatus> {
  const response = await request(
    `${threadsBase()}/mine/${encodeURIComponent(threadId)}`,
    { method: "DELETE" },
  );
  return readSharingResponse<ThreadShareStatus>(response);
}

export async function fetchSharedThread(
  stack: string,
  threadId: string,
): Promise<SharedThreadPayload> {
  const response = await request(
    `${threadsBase()}/${encodeURIComponent(stack)}/${encodeURIComponent(threadId)}`,
  );
  return readSharingResponse<SharedThreadPayload>(response);
}

// ------------------------------------------------------------------ files --

export async function fetchFileShareStatus(
  threadId: string,
  path: string,
): Promise<FileShareStatus> {
  const response = await request(
    `${filesBase()}/mine/${encodeURIComponent(threadId)}?path=${encodeURIComponent(path)}`,
  );
  return readSharingResponse<FileShareStatus>(response);
}

export async function internalizeFile(
  threadId: string,
  path: string,
  title?: string,
): Promise<FileShareStatus> {
  const response = await request(
    `${filesBase()}/mine/${encodeURIComponent(threadId)}`,
    jsonInit("POST", title ? { path, title } : { path }),
  );
  return readSharingResponse<FileShareStatus>(response);
}

export async function revokeFileShare(
  threadId: string,
  path: string,
): Promise<FileShareStatus> {
  const response = await request(
    `${filesBase()}/mine/${encodeURIComponent(threadId)}?path=${encodeURIComponent(path)}`,
    { method: "DELETE" },
  );
  return readSharingResponse<FileShareStatus>(response);
}
