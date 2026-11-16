"""MCP OAuth discovery stays on the official HTTPS origin."""
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from portfolio_tracker import integration_auth

RESOURCE = "https://mcp.indmoney.com/mcp"
PROTECTED = "https://mcp.indmoney.com/.well-known/oauth-protected-resource/mcp"
AUTH_META = "https://mcp.indmoney.com/.well-known/oauth-authorization-server"
ISSUER = "https://mcp.indmoney.com/"
AUTH = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "authorize",
        "token_endpoint": ISSUER + "token", "registration_endpoint": ISSUER + "register",
        "scopes_supported": ["portfolio:read", "market:read", "insights:write"]}


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.payload).encode()


class Opener:
    def __init__(self, responses): self.responses, self.seen = responses, []
    def urlopen(self, request, timeout):
        url = request.full_url
        self.seen.append(url)
        if url not in self.responses:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return Response(self.responses[url])


def test_indmoney_path_resource_discovers_authorization_server():
    opener = Opener({PROTECTED: {"resource": RESOURCE, "authorization_servers": [ISSUER],
                                "scopes_supported": ["portfolio:read", "market:read", "insights:write"]},
                     AUTH_META: AUTH})
    assert integration_auth.fetch_metadata(RESOURCE, opener=opener) == AUTH
    assert opener.seen == [PROTECTED, AUTH_META]


def test_metadata_requests_identify_http_client_without_python_default():
    class CheckingOpener(Opener):
        def urlopen(self, request, timeout):
            assert request.get_header("User-agent") == "Mozilla/5.0"
            return super().urlopen(request, timeout)
    opener = CheckingOpener({PROTECTED: {"resource": RESOURCE, "authorization_servers": [ISSUER]}, AUTH_META: AUTH})
    assert integration_auth.fetch_metadata(RESOURCE, opener=opener)["issuer"] == ISSUER


def test_registration_and_token_requests_use_provider_compatible_user_agent(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"client_id":"public"}'
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("User-agent") == "Mozilla/5.0"
            return Response()
    monkeypatch.setattr(integration_auth.urllib.request, "build_opener", lambda *args: Opener())
    assert integration_auth._post_json("https://mcp.upstox.com/register", {"client_name": "test"}) == {"client_id": "public"}
    assert integration_auth._post("https://mcp.upstox.com/token", {"grant_type": "authorization_code"}) == {"client_id": "public"}


def test_upstox_base_resource_metadata_is_accepted_only_for_upstox():
    endpoint = "https://mcp.upstox.com/mcp"
    protected = "https://mcp.upstox.com/.well-known/oauth-protected-resource/mcp"
    auth_url = "https://mcp.upstox.com/.well-known/oauth-authorization-server"
    auth = {"issuer": "https://mcp.upstox.com", "authorization_endpoint": "https://mcp.upstox.com/authorize", "token_endpoint": "https://mcp.upstox.com/token"}
    opener = Opener({protected: {"resource": "https://mcp.upstox.com", "authorization_servers": [auth["issuer"]]}, auth_url: auth})
    assert integration_auth.fetch_metadata(endpoint, opener=opener) == {**auth, "scopes_supported": None}
    assert opener.seen == [protected, auth_url]


def test_upstox_rejects_arbitrary_resource_on_its_origin():
    endpoint = "https://mcp.upstox.com/mcp"
    protected = "https://mcp.upstox.com/.well-known/oauth-protected-resource/mcp"
    opener = Opener({protected: {"resource": "https://mcp.upstox.com/other", "authorization_servers": ["https://mcp.upstox.com"]}})
    with pytest.raises(RuntimeError, match="resource"):
        integration_auth.fetch_metadata(endpoint, opener=opener)
    assert opener.seen == [protected]


def test_kite_root_protected_resource_fallback_preserves_scope_metadata():
    endpoint = "https://mcp.kite.trade/mcp"
    path = "https://mcp.kite.trade/.well-known/oauth-protected-resource/mcp"
    root = "https://mcp.kite.trade/.well-known/oauth-protected-resource"
    auth_url = "https://mcp.kite.trade/.well-known/oauth-authorization-server"
    auth = {"issuer": "https://mcp.kite.trade", "authorization_endpoint": "https://mcp.kite.trade/authorize", "token_endpoint": "https://mcp.kite.trade/token"}
    opener = Opener({root: {"resource": endpoint, "authorization_servers": [auth["issuer"]], "scopes_supported": ["default"]}, auth_url: auth})
    assert integration_auth.fetch_metadata(endpoint, opener=opener)["scopes_supported"] == ["default"]
    assert opener.seen == [path, root, auth_url]


def test_root_authorization_metadata_fallback_when_resource_metadata_unavailable():
    opener = Opener({AUTH_META: AUTH})
    assert integration_auth.fetch_metadata(RESOURCE, opener=opener) == AUTH
    assert opener.seen == [PROTECTED, "https://mcp.indmoney.com/.well-known/oauth-protected-resource", AUTH_META]


def test_same_origin_metadata_override_is_supported():
    url = "https://mcp.indmoney.com/metadata"
    opener = Opener({url: AUTH})
    assert integration_auth.fetch_metadata(RESOURCE, metadata_url=url, opener=opener) == AUTH
    assert opener.seen == [url]


def test_resource_mismatch_rejected_without_authorization_server_fallback():
    opener = Opener({PROTECTED: {"resource": "https://evil.example/mcp", "authorization_servers": [ISSUER]}, AUTH_META: AUTH})
    with pytest.raises(RuntimeError, match="resource"):
        integration_auth.fetch_metadata(RESOURCE, opener=opener)
    assert opener.seen == [PROTECTED]


@pytest.mark.parametrize("issuer", ["http://127.0.0.1:9999", "https://127.0.0.1:9999", "https://evil.example"])
def test_untrusted_authorization_server_issuer_never_fetched(issuer):
    opener = Opener({PROTECTED: {"resource": RESOURCE, "authorization_servers": [issuer]}, AUTH_META: AUTH})
    with pytest.raises(RuntimeError, match="issuer|authorization server"):
        integration_auth.fetch_metadata(RESOURCE, opener=opener)
    assert opener.seen == [PROTECTED]


def test_authorization_server_issuer_must_match_requested_issuer():
    opener = Opener({PROTECTED: {"resource": RESOURCE, "authorization_servers": [ISSUER]},
                     AUTH_META: {**AUTH, "issuer": "https://evil.example/"}})
    with pytest.raises(RuntimeError, match="issuer"):
        integration_auth.fetch_metadata(RESOURCE, opener=opener)
    assert opener.seen == [PROTECTED, AUTH_META]


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "registration_endpoint"])
def test_discovered_endpoints_must_remain_on_official_https_origin(field):
    opener = Opener({PROTECTED: {"resource": RESOURCE, "authorization_servers": [ISSUER]},
                     AUTH_META: {**AUTH, field: "http://127.0.0.1:9999/steal"}})
    with pytest.raises(RuntimeError, match="endpoint"):
        integration_auth.fetch_metadata(RESOURCE, opener=opener)


@pytest.mark.parametrize("url", ["http://127.0.0.1:9999/metadata", "https://evil.example/metadata", "https://mcp.indmoney.com@127.0.0.1/metadata"])
def test_override_metadata_url_must_be_official_https_origin(url):
    opener = Opener({url: AUTH})
    with pytest.raises(RuntimeError, match="metadata|URL"):
        integration_auth.fetch_metadata(RESOURCE, metadata_url=url, opener=opener)
    assert opener.seen == []


@pytest.mark.parametrize("poster,form", [(integration_auth._post, {"grant_type": "refresh_token"}),
                                     (integration_auth._post_json, {"client_name": "x"})])
def test_token_and_registration_posts_reject_untrusted_urls_without_network(poster, form, monkeypatch):
    def fail(*args, **kwargs): raise AssertionError("network request made")
    monkeypatch.setattr(integration_auth.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match="URL|endpoint"):
        poster("http://127.0.0.1:9999/steal", form)


@pytest.mark.parametrize("provider,advertised,expected", [
    ("indmoney", ["insights:write", "portfolio:read", "market:read"], ["portfolio:read market:read"]),
    ("indmoney", ["insights:write"], []),
    ("kite", ["default", "orders:write"], []),
    ("upstox", ["orders:write"], []),
])
def test_browser_authorization_never_requests_unapproved_scopes(monkeypatch, tmp_path, provider, advertised, expected):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "integration.enc"))
    origin = integration_auth.OFFICIAL_MCP_ENDPOINTS[provider]["url"].removesuffix("/mcp")
    url = integration_auth.begin_oauth("test-secret", provider, session_id="sid", redirect_uri="https://app.test/callback",
        metadata={"client_id": "cid", "authorization_endpoint": origin + "/authorize", "scopes_supported": advertised})
    assert parse_qs(urlsplit(url).query).get("scope", []) == expected


@pytest.mark.parametrize("provider", ["kite", "upstox", "indmoney"])
def test_browser_authorization_rejects_cross_provider_and_insecure_urls(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "integration.enc"))
    for url in ("https://evil.example/authorize", "http://mcp.upstox.com/authorize", "https://mcp.upstox.com.evil.example/authorize"):
        with pytest.raises(RuntimeError, match="URL|endpoint"):
            integration_auth.begin_oauth("test-secret", provider, session_id="sid", redirect_uri="https://app.test/callback",
                metadata={"client_id": "cid", "authorization_endpoint": url})
    other = "https://mcp.kite.trade/authorize" if provider != "kite" else "https://mcp.upstox.com/authorize"
    with pytest.raises(RuntimeError, match="URL|endpoint"):
        integration_auth.begin_oauth("test-secret", provider, session_id="sid", redirect_uri="https://app.test/callback",
            metadata={"client_id": "cid", "authorization_endpoint": other})


@pytest.mark.parametrize("poster,form", [(integration_auth._post, {"grant_type": "authorization_code"}),
                                     (integration_auth._post_json, {"client_name": "test"})])
def test_registration_and_token_posts_refuse_redirects(poster, form, monkeypatch):
    class RedirectingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://evil.example/steal"}, None)
    monkeypatch.setattr(integration_auth.urllib.request, "build_opener", lambda *args: RedirectingOpener())
    with pytest.raises(RuntimeError, match="HTTP 302|redirect"):
        poster("https://mcp.upstox.com/token", form)


def test_callback_rejects_foreign_token_endpoint_before_consuming_state(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "integration.enc"))
    secret = "test-secret"
    url = integration_auth.begin_oauth(secret, "upstox", session_id="sid", redirect_uri="https://app.test/callback",
        metadata={"client_id": "cid", "authorization_endpoint": "https://mcp.upstox.com/authorize"})
    state = parse_qs(urlsplit(url).query)["state"][0]
    monkeypatch.setattr(integration_auth, "_post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network POST attempted")))
    with pytest.raises(RuntimeError, match="URL|endpoint"):
        integration_auth.complete_oauth(secret, "upstox", state=state, code="code", session_id="sid", token_endpoint="https://mcp.kite.trade/token")
    assert state in integration_auth._load(secret)["oauth"]
