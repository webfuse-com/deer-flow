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

[argus patch #55] Trusting the edge's identity is not the same as accepting it.
On a single-citizen ``atlas-<name>`` stack the run executes AS the owner -- the
knowledge-ring caller token, the Gmail/GitHub/Asana credentials and the
read-write sandbox mounts all belong to them -- so a *different* citizen
arriving here must not be auto-provisioned an account, whatever the edge says.
The Caddy owner gate is the primary control; this is the second layer, and the
one that also covers a direct argus-net/tailnet caller who holds the proxy
secret. The exception is the shared-app API: apps are shareable between citizens
by design, and Caddy routes ``/api/apps/*`` from the ``apps-<owner>`` origin
into the owner's gateway, so those prefixes stay open to any authenticated
citizen.
"""

from __future__ import annotations

import os
import secrets

SSO_EMAIL_HEADER_NAME = "X-Auth-Email"
SSO_PROXY_SECRET_HEADER_NAME = "X-Auth-Proxy-Secret"
SSO_PROXY_SECRET_ENV_VAR = "DEER_FLOW_SSO_PROXY_SECRET"

_PROXY_SECRET = os.environ.get(SSO_PROXY_SECRET_ENV_VAR, "")

# [argus patch #55] Owner resolution, in preference order. ATLAS_STACK_OWNER_EMAIL
# is the explicit declaration; PYTHIA_CALLER_EMAIL is read as a fallback because
# it already carries the citizen's address on every atlas stack (it is the
# identity the knowledge-ring caller token is signed with, so if it is wrong the
# stack has bigger problems than this check).
STACK_OWNER_ENV_VARS = ("ATLAS_STACK_OWNER_EMAIL", "PYTHIA_CALLER_EMAIL")

# Only single-citizen stacks are owner-gated. `pythia` and any other shared
# project are legitimately multi-citizen and carry no owner, so they are exempt
# BY NAME rather than by the accident of an unset variable.
SINGLE_CITIZEN_PROJECT_PREFIX = "atlas-"

# Prefixes a non-owner citizen may still reach. These are the shared-app API
# routes Caddy proxies in from the apps-<owner> origin (@apps_api in the
# Caddyfile); anything else -- threads, runs, memory, skills, MCP config -- is
# the owner's session and is denied.
GUEST_PATH_PREFIXES = ("/api/apps/", "/api/connectors/", "/api/transformers/")


def stack_owner_email() -> str | None:
    """The citizen this stack belongs to, or None if it is not owner-gated."""
    for var in STACK_OWNER_ENV_VARS:
        value = (os.environ.get(var) or "").strip().lower()
        if value:
            return value
    return None


def is_single_citizen_stack() -> bool:
    project = (os.environ.get("ARGUS_PROJECT") or "").strip().lower()
    return project.startswith(SINGLE_CITIZEN_PROJECT_PREFIX)


def sso_email_allowed(email: str, path: str) -> bool:
    """Whether an edge-verified SSO identity may act on this stack at ``path``.

    Shared (non ``atlas-*``) projects are always allowed -- they have no single
    owner to compare against. On a single-citizen stack the owner is allowed
    everywhere and everyone else only on the shared-app prefixes.

    Fail-closed on misconfiguration: an ``atlas-*`` stack that declares no owner
    denies SSO trust outright rather than falling back to "anyone authenticated".
    The citizen can still reach the local password login, which is the
    documented break-glass path.
    """
    if not is_single_citizen_stack():
        return True
    owner = stack_owner_email()
    if owner is None:
        return False
    if (email or "").strip().lower() == owner:
        return True
    return path.startswith(GUEST_PATH_PREFIXES)


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
