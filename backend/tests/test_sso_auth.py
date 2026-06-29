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
