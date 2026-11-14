from datetime import datetime, timedelta, timezone

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
    token = csrf(c)
    response = c.post("/api/integrations/kite/connect", headers={"X-CSRF-Token": token})
    assert response.status_code == 409
    assert response.get_json()["status"] == "authorization_required"
    assert response.get_json()["mcp_endpoint"] == "https://mcp.kite.trade/mcp"
    with c.session_transaction() as sess:
        assert "oauth_state" not in sess


def test_mcp_callback_requires_authorization_state_and_disconnect(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    assert c.get("/oauth/upstox/callback?code=code").status_code == 400
    token = csrf(c)
    assert c.post("/api/integrations/upstox/disconnect", headers={"X-CSRF-Token": token}).status_code == 200


def test_indmoney_handoff_and_write_rejected(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    handoff = c.get("/integrations/indmoney/connect")
    assert handoff.status_code == 200
    assert "https://mcp.indmoney.com/mcp" in handoff.get_data(as_text=True)
    rejected = c.post("/api/integrations/indmoney/write", headers={"X-CSRF-Token": csrf(c)})
    assert rejected.status_code == 405
    assert c.get("/api/integrations").get_json()["integrations"]["indmoney"]["connected"] is False
