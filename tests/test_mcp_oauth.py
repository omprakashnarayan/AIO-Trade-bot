import json
from datetime import datetime, timedelta, timezone

from portfolio_tracker import integration_auth
from portfolio_tracker.provider_mcp import MCPClient


class Resp:
    def __init__(self, data, headers=None): self.data, self.headers = data, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.data).encode()


def test_pkce_state_storage_callback_and_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "i.enc"))
    secret = "session-secret"
    meta = {"authorization_endpoint": "https://auth.example/authorize", "token_endpoint": "https://auth.example/token", "registration_endpoint": "https://auth.example/register"}
    url = integration_auth.begin_oauth(secret, "indmoney", session_id="sid", redirect_uri="https://app/cb", metadata={**meta, "client_id": "cid"})
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert integration_auth.status(secret, "indmoney")["connected"] is False
    monkeypatch.setattr(integration_auth, "_post", lambda *a, **k: {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
    result = integration_auth.complete_oauth(secret, "indmoney", state=query["state"][0], code="code", session_id="sid", token_endpoint=meta["token_endpoint"])
    assert result["oauth_authorized"] is True and result["connected"] is False
    assert "access" not in integration_auth.redact({"access_token": "access"})["access_token"]
    assert "access" not in (tmp_path / "i.enc").read_text()


def test_mcp_client_injects_authorization(monkeypatch):
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request.headers.get("Authorization"))
            return Resp({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "snapshot", "inputSchema": {"type": "object"}}]}})
    client = MCPClient("https://mcp.example/mcp", provider="indmoney", access_token="token-value", opener=Opener())
    client.initialized = True
    client.discover()
    assert seen == ["Bearer token-value"]
def test_browser_route_uses_metadata_registration_and_callback(monkeypatch, tmp_path):
    from werkzeug.security import generate_password_hash
    from portfolio_tracker import web
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "route.enc"))
    monkeypatch.setattr(integration_auth, "fetch_metadata", lambda *a, **k: {"authorization_endpoint": "https://auth.example/authorize", "token_endpoint": "https://auth.example/token", "registration_endpoint": "https://auth.example/register"})
    monkeypatch.setattr(integration_auth, "_post_json", lambda *a, **k: {"client_id": "dynamic-public-client"})
    monkeypatch.setattr(integration_auth, "_post", lambda *a, **k: {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
    c = web.app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    with c.session_transaction() as sess: csrf = sess["csrf_token"]
    started = c.post("/api/integrations/indmoney/connect", headers={"X-CSRF-Token": csrf})
    assert started.status_code == 302 and "code_challenge=" in started.location
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(started.location).query)["state"][0]
    done = c.get(f"/oauth/indmoney/callback?state={state}&code=code")
    assert done.status_code == 302
    status = c.get("/api/integrations").get_json()["integrations"]["indmoney"]
    assert status["oauth_authorized"] is True and status["connected"] is False


def test_expired_token_refreshes_without_logging(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "refresh.enc"))
    secret = "refresh-secret"
    integration_auth._store_tokens(secret, "indmoney", {"access_token": "old", "refresh_token": "refresh", "expires_in": 0}, session_id="sid")
    token = integration_auth.access_token(secret, "indmoney", session_id="sid", refresh=lambda value: {"access_token": "new", "refresh_token": value, "expires_in": 3600})
    assert token == "new"
    assert "new" not in (tmp_path / "refresh.enc").read_text()

