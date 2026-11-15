"""MCP OAuth discovery stays on the official HTTPS origin."""
import json
import urllib.error

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


def test_root_authorization_metadata_fallback_when_resource_metadata_unavailable():
    opener = Opener({AUTH_META: AUTH})
    assert integration_auth.fetch_metadata(RESOURCE, opener=opener) == AUTH
    assert opener.seen == [PROTECTED, AUTH_META]


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
