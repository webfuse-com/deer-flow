"""Trusted-proxy SSO authentication (Argus / Caddy edge).

The Caddy edge authenticates every browser request via Google SSO (oauth2-proxy)
and injects a VERIFIED, spoof-protected ``X-Auth-Email`` header (Caddy strips any
client-supplied copy before injecting its own). This module lets the gateway
trust that header IN PLACE OF the DeerFlow password login, so a citizen who has
already passed Google SSO at the edge is not asked to log in a second time.

Security: the gateway is also reachable on the tailnet, BYPASSING Caddy, so a
direct tailnet caller could forge ``X-Auth-Email``. We therefore trust the email
ONLY when the request also carries a shared proxy secret (``X-Auth-Proxy-Secret``)
that Caddy injects and a direct caller does not know — validated with a
constant-time compare. This mirrors the Lexis trusted-proxy pattern and the
existing ``X-DeerFlow-Internal-Token`` gate. With no secret configured, SSO
header trust is DISABLED (fail-closed): the gateway falls back to cookie/login.
"""

from __future__ import annotations

import os
import secrets

SSO_EMAIL_HEADER_NAME = "X-Auth-Email"
SSO_PROXY_SECRET_HEADER_NAME = "X-Auth-Proxy-Secret"
SSO_PROXY_SECRET_ENV_VAR = "DEER_FLOW_SSO_PROXY_SECRET"

_PROXY_SECRET = os.environ.get(SSO_PROXY_SECRET_ENV_VAR, "")


def sso_trust_enabled() -> bool:
    """SSO-header trust is active only when a proxy secret is configured."""
    return bool(_PROXY_SECRET)


def trusted_sso_email(headers) -> str | None:
    """Return the verified SSO email IFF the request carries a matching proxy
    secret, else None. ``headers`` is the request headers mapping.

    Fail-closed: no configured secret, no/empty email, or a mismatched secret
    all return None so the caller falls back to cookie/login auth.
    """
    if not _PROXY_SECRET:
        return None
    presented = headers.get(SSO_PROXY_SECRET_HEADER_NAME) or ""
    if not secrets.compare_digest(presented, _PROXY_SECRET):
        return None
    email = (headers.get(SSO_EMAIL_HEADER_NAME) or "").strip().lower()
    return email or None
