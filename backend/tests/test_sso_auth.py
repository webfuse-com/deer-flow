"""[argus patch #15/#16] Tests for trusted-proxy SSO header auth.

The Caddy edge authenticates the browser via Google SSO and injects a verified
X-Auth-Email plus a shared proxy secret; the gateway trusts the email IFF the
secret matches (constant-time), so a direct tailnet caller cannot forge it.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_sso(monkeypatch, secret: str | None):
    """Reload sso_auth with the env secret set/unset so the module-level
    _PROXY_SECRET picks it up."""
    if secret is None:
        monkeypatch.delenv("DEER_FLOW_SSO_PROXY_SECRET", raising=False)
    else:
        monkeypatch.setenv("DEER_FLOW_SSO_PROXY_SECRET", secret)
    import app.gateway.sso_auth as sso_auth

    return importlib.reload(sso_auth)


class TestTrustedSsoEmail:
    def test_disabled_without_secret(self, monkeypatch):
        sso = _reload_sso(monkeypatch, None)
        assert sso.sso_trust_enabled() is False
        # Even with a valid-looking email + secret header, trust is off.
        headers = {"X-Auth-Email": "alice@surfly.com", "X-Auth-Proxy-Secret": "anything"}
        assert sso.trusted_sso_email(headers) is None

    def test_matching_secret_returns_lowercased_email(self, monkeypatch):
        sso = _reload_sso(monkeypatch, "topsecret")
        assert sso.sso_trust_enabled() is True
        headers = {"X-Auth-Email": "Alice@Surfly.com", "X-Auth-Proxy-Secret": "topsecret"}
        assert sso.trusted_sso_email(headers) == "alice@surfly.com"

    def test_wrong_secret_returns_none(self, monkeypatch):
        sso = _reload_sso(monkeypatch, "topsecret")
        headers = {"X-Auth-Email": "alice@surfly.com", "X-Auth-Proxy-Secret": "wrong"}
        assert sso.trusted_sso_email(headers) is None

    def test_missing_secret_header_returns_none(self, monkeypatch):
        sso = _reload_sso(monkeypatch, "topsecret")
        headers = {"X-Auth-Email": "alice@surfly.com"}
        assert sso.trusted_sso_email(headers) is None

    def test_empty_email_returns_none(self, monkeypatch):
        sso = _reload_sso(monkeypatch, "topsecret")
        headers = {"X-Auth-Email": "  ", "X-Auth-Proxy-Secret": "topsecret"}
        assert sso.trusted_sso_email(headers) is None

    @pytest.fixture(autouse=True)
    def _restore(self, monkeypatch):
        # Ensure other test modules see sso_auth in its env-default state.
        yield
        monkeypatch.delenv("DEER_FLOW_SSO_PROXY_SECRET", raising=False)
        import app.gateway.sso_auth as sso_auth

        importlib.reload(sso_auth)


def _reload_sso_owner(monkeypatch, *, project: str | None, owner: str | None, pythia_caller: str | None = None):
    """Reload sso_auth with the stack-identity env in a known state."""
    for var, value in (
        ("ARGUS_PROJECT", project),
        ("ATLAS_STACK_OWNER_EMAIL", owner),
        ("PYTHIA_CALLER_EMAIL", pythia_caller),
    ):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    import app.gateway.sso_auth as sso_auth

    return importlib.reload(sso_auth)


class TestSsoEmailAllowed:
    """[argus patch #55] Authenticated is not authorized.

    A single-citizen stack runs AS its owner: the knowledge-ring caller token,
    the Gmail/GitHub/Asana credentials and the read-write sandbox mounts are all
    theirs. So a verified SSO identity belonging to someone else must be refused
    even though the edge vouched for it.
    """

    OWNER = "quinten@surfly.com"
    OTHER = "nicholas@surfly.com"

    def test_owner_allowed_everywhere(self, monkeypatch):
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=self.OWNER)
        for path in ("/", "/api/threads", "/api/memory", "/api/skills", "/api/apps/x"):
            assert sso.sso_email_allowed(self.OWNER, path) is True, path

    def test_non_owner_denied_on_the_owner_session(self, monkeypatch):
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=self.OWNER)
        for path in ("/", "/api/threads", "/api/threads/abc/runs/stream", "/api/memory", "/api/skills", "/api/mcp"):
            assert sso.sso_email_allowed(self.OTHER, path) is False, path

    def test_non_owner_allowed_on_the_shared_app_api(self, monkeypatch):
        # Apps are shareable between citizens, and Caddy routes /api/apps/* from
        # the apps-<owner> origin into the owner's gateway. Denying these would
        # break every shared app on the box.
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=self.OWNER)
        for path in ("/api/apps/", "/api/apps/forecast/data", "/api/connectors/x", "/api/transformers/x"):
            assert sso.sso_email_allowed(self.OTHER, path) is True, path

    def test_shared_projects_are_not_owner_gated(self, monkeypatch):
        # `pythia` is legitimately multi-citizen and carries no owner. It is
        # exempt by NAME, so an unset owner variable there is not silently
        # reinterpreted as "allow everyone" on an atlas stack.
        sso = _reload_sso_owner(monkeypatch, project="pythia", owner=None)
        assert sso.sso_email_allowed(self.OTHER, "/api/threads") is True

    def test_atlas_stack_without_an_owner_fails_closed(self, monkeypatch):
        # A misconfigured atlas stack denies SSO rather than admitting anyone.
        # The local password login remains as break-glass.
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=None)
        assert sso.sso_email_allowed(self.OWNER, "/api/threads") is False
        assert sso.sso_email_allowed(self.OTHER, "/api/threads") is False

    def test_pythia_caller_email_is_the_fallback_owner(self, monkeypatch):
        # Every atlas stack already carries the citizen's address here, so the
        # patch is effective before ATLAS_STACK_OWNER_EMAIL is rolled out.
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=None, pythia_caller=self.OWNER)
        assert sso.sso_email_allowed(self.OWNER, "/api/threads") is True
        assert sso.sso_email_allowed(self.OTHER, "/api/threads") is False

    def test_explicit_owner_wins_over_the_fallback(self, monkeypatch):
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=self.OWNER, pythia_caller="stale@surfly.com")
        assert sso.sso_email_allowed(self.OWNER, "/api/threads") is True
        assert sso.sso_email_allowed("stale@surfly.com", "/api/threads") is False

    @pytest.mark.parametrize(
        "stored,presented",
        [
            ("quinten@surfly.com", "Quinten@Surfly.com"),
            ("Quinten@Surfly.com", "quinten@surfly.com"),
        ],
    )
    def test_owner_comparison_is_case_insensitive(self, monkeypatch, stored, presented):
        # oauth2-proxy is not contractually obliged to lowercase the claim. If
        # this became case-sensitive the OWNER would be locked out of their own
        # stack, which is the failure most likely to be blamed on something else.
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=stored)
        assert sso.sso_email_allowed(presented, "/api/threads") is True

    def test_project_prefix_is_matched_not_substring(self, monkeypatch):
        # A project merely CONTAINING "atlas-" must not be treated as a citizen
        # stack, and one that is must not escape the gate by casing.
        sso = _reload_sso_owner(monkeypatch, project="ATLAS-Quinten", owner=self.OWNER)
        assert sso.sso_email_allowed(self.OTHER, "/api/threads") is False
        sso = _reload_sso_owner(monkeypatch, project="shared-atlas-tools", owner=None)
        assert sso.sso_email_allowed(self.OTHER, "/api/threads") is True

    def test_guest_prefix_cannot_be_faked_by_a_suffix(self, monkeypatch):
        sso = _reload_sso_owner(monkeypatch, project="atlas-quinten", owner=self.OWNER)
        for path in ("/api/threads/api/apps/", "/x/api/apps/", "/api/appsomething"):
            assert sso.sso_email_allowed(self.OTHER, path) is False, path
