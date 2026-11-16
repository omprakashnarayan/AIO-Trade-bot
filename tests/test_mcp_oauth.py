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
    meta = {"authorization_endpoint": "https://mcp.indmoney.com/authorize", "token_endpoint": "https://mcp.indmoney.com/token", "registration_endpoint": "https://mcp.indmoney.com/register"}
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
    monkeypatch.setattr(integration_auth, "fetch_metadata", lambda *a, **k: {"authorization_endpoint": "https://mcp.indmoney.com/authorize", "token_endpoint": "https://mcp.indmoney.com/token", "registration_endpoint": "https://mcp.indmoney.com/register"})
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


def test_public_oauth_callback_stays_https_behind_http_tunnel(monkeypatch, tmp_path):
    from werkzeug.security import generate_password_hash
    from portfolio_tracker import web
    monkeypatch.setenv("PORTFOLIO_PUBLIC_HTTPS", "1")
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "oauth.enc"))
    monkeypatch.setattr(integration_auth, "fetch_metadata", lambda *a, **k: {
        "authorization_endpoint": "https://mcp.indmoney.com/authorize", "token_endpoint": "https://mcp.indmoney.com/token",
        "registration_endpoint": "https://mcp.indmoney.com/register"})
    registered = []
    def register(url, payload):
        registered.append(payload["redirect_uris"][0])
        return {"client_id": "client"}
    monkeypatch.setattr(integration_auth, "_post_json", register)
    c = web.app.test_client()
    assert c.post("/login", base_url="http://findash.deepsort.in", data={"username":"admin","password":"secret"}).status_code == 302
    r = c.get("/integrations/indmoney/start", base_url="http://findash.deepsort.in")
    assert r.status_code == 302
    from urllib.parse import parse_qs, urlsplit
    assert registered == ["https://findash.deepsort.in/oauth/indmoney/callback"]
    assert parse_qs(urlsplit(r.location).query)["redirect_uri"] == registered


def test_expired_token_refreshes_without_logging(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "refresh.enc"))
    secret = "refresh-secret"
    integration_auth._store_tokens(secret, "indmoney", {"access_token": "old", "refresh_token": "refresh", "expires_in": 0}, session_id="sid")
    token = integration_auth.access_token(secret, "indmoney", session_id="sid", refresh=lambda value: {"access_token": "new", "refresh_token": value, "expires_in": 3600})
    assert token == "new"
    assert "new" not in (tmp_path / "refresh.enc").read_text()


def test_authenticated_operator_can_reuse_saved_provider_consent_after_login(monkeypatch, tmp_path):
    from werkzeug.security import generate_password_hash
    from portfolio_tracker import web
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "saved-consent.enc"))
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    integration_auth._store_tokens(str(web.app.secret_key), "upstox", {"access_token":"saved-token", "expires_in":3600}, session_id="previous-login")
    c = web.app.test_client()
    assert c.post("/login",data={"username":"admin","password":"secret"}).status_code == 302
    with c.session_transaction() as sess:
        assert sess["oauth_session_id"] != "previous-login"
        csrf = sess["csrf_token"]
    class DB:
        def init_db(self): pass
    class Service:
        def __init__(self, db): pass
        def sync(self, adapter):
            assert adapter.client.token_provider() == "saved-token"
            adapter.client.tools = {"get-holdings": object(), "get-positions": object()}
            return {"status":"succeeded", "counts":{"holdings":1}}
    monkeypatch.setattr(web, "Database", DB)
    monkeypatch.setattr(web, "ProviderSyncService", Service)
    r = c.post("/api/integrations/upstox/sync",headers={"X-CSRF-Token":csrf})
    assert r.status_code == 200
    assert integration_auth.status(str(web.app.secret_key),"upstox")["connected"] is True

