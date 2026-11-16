from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from werkzeug.security import generate_password_hash

from portfolio_tracker import integration_auth, web


def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "integrations.enc"))
    c = web.app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return c


def csrf(c):
    with c.session_transaction() as sess:
        return sess["csrf_token"]


def test_config_requires_csrf_and_redacts_secrets(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    payload = {"api_key": "kite-key", "api_secret": "kite-secret", "redirect_uri": "https://example.test/oauth/kite/callback"}
    assert c.post("/api/integrations/kite/config", json=payload).status_code == 403
    response = c.post("/api/integrations/kite/config", json=payload, headers={"X-CSRF-Token": csrf(c)})
    assert response.status_code == 200
    body = response.get_json()
    assert "kite-secret" not in response.get_data(as_text=True)
    assert body["configured"] is True
    raw = (tmp_path / "integrations.enc").read_text()
    assert "kite-secret" not in raw


def test_mcp_connect_never_starts_legacy_oauth(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    monkeypatch.setattr(integration_auth, "fetch_metadata", lambda *args, **kwargs: {
        "authorization_endpoint": "https://mcp.kite.trade/authorize",
        "token_endpoint": "https://mcp.kite.trade/token",
        "registration_endpoint": "https://mcp.kite.trade/register",
        "scopes_supported": ["default"],
    })
    monkeypatch.setattr(integration_auth, "_post_json", lambda *args, **kwargs: {"client_id": "public-mcp-client"})
    token = csrf(c)
    response = c.post("/api/integrations/kite/connect", headers={"X-CSRF-Token": token})
    assert response.status_code == 302
    target = urlsplit(response.headers["Location"])
    assert (target.scheme, target.netloc, target.path) == ("https", "mcp.kite.trade", "/authorize")
    query = parse_qs(target.query)
    assert query["client_id"] == ["public-mcp-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query
    with c.session_transaction() as sess:
        assert "oauth_state" not in sess


def test_mcp_callback_requires_authorization_state_and_disconnect(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    assert c.get("/oauth/upstox/callback?code=code").status_code == 400
    token = csrf(c)
    assert c.post("/api/integrations/upstox/disconnect", headers={"X-CSRF-Token": token}).status_code == 200


def test_legacy_api_token_is_not_mcp_authorization(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(tmp_path / "integrations.enc"))
    integration_auth._save(str(web.app.secret_key), {"providers": {"kite": {"access_token": "legacy-only"}}})
    state = integration_auth.status(str(web.app.secret_key), "kite")
    assert state["oauth_authorized"] is False
    assert state["connected"] is False
    assert state["status"] == "authorization_required"


def test_integrations_page_has_only_mcp_controls():
    template = web.OPERATOR_PAGE_TEMPLATE
    assert 'Authorize / reconnect in browser' in template
    assert 'Discover + sync verified read' in template
    assert 'Save encrypted config' not in template
    assert 'Start OAuth' not in template
    assert 'Official MCP handoff' not in template


def test_indmoney_handoff_and_write_rejected(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    handoff = c.get("/integrations/indmoney/connect")
    assert handoff.status_code == 200
    assert "https://mcp.indmoney.com/mcp" in handoff.get_data(as_text=True)
    rejected = c.post("/api/integrations/indmoney/write", headers={"X-CSRF-Token": csrf(c)})
    assert rejected.status_code == 405
    assert c.get("/api/integrations").get_json()["integrations"]["indmoney"]["connected"] is False
