import { cookies, headers } from "next/headers";

import { isStaticWebsiteOnly } from "../static-mode";

import { getGatewayConfig } from "./gateway-config";
import { STATIC_WEBSITE_USER } from "./static-user";
import { type AuthResult, userSchema } from "./types";

const SSR_AUTH_TIMEOUT_MS = 5_000;

/**
 * Fetch the authenticated user from the gateway using the request's cookies.
 * Returns a tagged AuthResult — callers use exhaustive switch, no try/catch.
 */
export async function getServerSideUser(): Promise<AuthResult> {
  if (isStaticWebsiteOnly()) {
    return {
      tag: "authenticated",
      user: STATIC_WEBSITE_USER,
    };
  }

  if (process.env.DEER_FLOW_AUTH_DISABLED === "1") {
    return {
      tag: "authenticated",
      user: {
        id: "e2e-user",
        email: "e2e@test.local",
        system_role: "admin",
        needs_setup: false,
      },
    };
  }

  const cookieStore = await cookies();
  const sessionCookie = cookieStore.get("access_token");

  // Trusted-proxy SSO: Caddy injects X-Auth-Email + X-Auth-Proxy-Secret on
  // edge-authenticated (Google SSO) browser requests. Forward them to the
  // gateway's /me so a citizen who already passed SSO is not bounced to
  // /login. The gateway only trusts the email when the proxy secret matches
  // (sso_auth.py), so forwarding works only from behind the edge.
  const reqHeaders = await headers();
  const ssoEmail = reqHeaders.get("x-auth-email");
  const ssoSecret = reqHeaders.get("x-auth-proxy-secret");
  const ssoHeaders: Record<string, string> = {};
  if (ssoEmail) ssoHeaders["X-Auth-Email"] = ssoEmail;
  if (ssoSecret) ssoHeaders["X-Auth-Proxy-Secret"] = ssoSecret;
  const hasSso = Boolean(ssoEmail && ssoSecret);

  let internalGatewayUrl: string;
  try {
    internalGatewayUrl = getGatewayConfig().internalGatewayUrl;
  } catch (err) {
    return { tag: "config_error", message: String(err) };
  }

  if (!sessionCookie && hasSso) {
    // No DeerFlow cookie but the edge proved identity via SSO — authenticate
    // off the forwarded headers instead of bouncing to /login.
    const ssoController = new AbortController();
    const ssoTimeout = setTimeout(
      () => ssoController.abort(),
      SSR_AUTH_TIMEOUT_MS,
    );
    try {
      const ssoRes = await fetch(`${internalGatewayUrl}/api/v1/auth/me`, {
        headers: ssoHeaders,
        cache: "no-store",
        signal: ssoController.signal,
      });
      clearTimeout(ssoTimeout);
      if (ssoRes.ok) {
        const parsedSso = userSchema.safeParse(await ssoRes.json());
        if (parsedSso.success) {
          if (parsedSso.data.needs_setup) {
            return { tag: "needs_setup", user: parsedSso.data };
          }
          return { tag: "authenticated", user: parsedSso.data };
        }
      }
    } catch {
      clearTimeout(ssoTimeout);
      // fall through to the normal no-session path below
    }
  }

  if (!sessionCookie) {
    // No session — check whether the system has been initialised yet.
    const setupController = new AbortController();
    const setupTimeout = setTimeout(
      () => setupController.abort(),
      SSR_AUTH_TIMEOUT_MS,
    );
    try {
      const setupRes = await fetch(
        `${internalGatewayUrl}/api/v1/auth/setup-status`,
        {
          cache: "no-store",
          signal: setupController.signal,
        },
      );
      clearTimeout(setupTimeout);
      if (setupRes.ok) {
        const setupData = (await setupRes.json()) as { needs_setup?: boolean };
        if (setupData.needs_setup) {
          return { tag: "system_setup_required" };
        }
      }
    } catch {
      clearTimeout(setupTimeout);
      // If setup-status is unreachable/times out, fall through to unauthenticated.
    }
    return { tag: "unauthenticated" };
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SSR_AUTH_TIMEOUT_MS);

  try {
    const res = await fetch(`${internalGatewayUrl}/api/v1/auth/me`, {
      headers: { Cookie: `access_token=${sessionCookie.value}`, ...ssoHeaders },
      cache: "no-store",
      signal: controller.signal,
    });
    clearTimeout(timeout); // Clear immediately — covers all response branches

    if (res.ok) {
      const parsed = userSchema.safeParse(await res.json());
      if (!parsed.success) {
        console.error("[SSR auth] Malformed /auth/me response:", parsed.error);
        return { tag: "gateway_unavailable" };
      }
      if (parsed.data.needs_setup) {
        return { tag: "needs_setup", user: parsed.data };
      }
      return { tag: "authenticated", user: parsed.data };
    }
    if (res.status === 401 || res.status === 403) {
      return { tag: "unauthenticated" };
    }
    console.error(`[SSR auth] /api/v1/auth/me responded ${res.status}`);
    return { tag: "gateway_unavailable" };
  } catch (err) {
    clearTimeout(timeout);
    console.error("[SSR auth] Failed to reach gateway:", err);
    return { tag: "gateway_unavailable" };
  }
}
