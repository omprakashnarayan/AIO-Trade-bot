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


def test_oauth_state_and_kite_exchange(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    token = csrf(c)
    c.post("/api/integrations/kite/config", json={"api_key": "key", "api_secret": "secret", "redirect_uri": "https://example.test/oauth/kite/callback"}, headers={"X-CSRF-Token": token})
    start = c.post("/api/integrations/kite/connect", headers={"X-CSRF-Token": token})
    assert start.status_code == 302
    with c.session_transaction() as sess:
        started_at = sess["oauth_state"]["started_at"]
    monkeypatch.setattr(integration_auth, "_post_form", lambda url, form, **kwargs: {"access_token": "access", "expires_in": 3600})
    # Kite's callback omits OAuth state; the authenticated browser session and
    # short start window bind the one-time request token instead.
    done = c.get("/oauth/kite/callback?request_token=req")
    assert done.status_code == 302
    status = c.get("/api/integrations").get_json()["integrations"]["kite"]
    assert status["connected"] is True
    assert status["last_authorized_at"]
    assert "access" not in c.get("/api/integrations").get_data(as_text=True)


def test_expired_callback_and_disconnect(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    token = csrf(c)
    c.post("/api/integrations/upstox/config", json={"client_id": "id", "client_secret": "secret", "redirect_uri": "https://example.test/oauth/upstox/callback"}, headers={"X-CSRF-Token": token})
    c.post("/api/integrations/upstox/connect", headers={"X-CSRF-Token": token})
    with c.session_transaction() as sess:
        state = sess["oauth_state"]["value"]
    monkeypatch.setattr(integration_auth, "_post_form", lambda url, form, **kwargs: {"access_token": "access", "expires_in": 1})
    assert c.get(f"/oauth/upstox/callback?state={state}&code=code").status_code == 302
    record = integration_auth.get_provider(str(web.app.secret_key), "upstox")
    record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    data = integration_auth._load(str(web.app.secret_key))
    data["providers"]["upstox"] = record
    integration_auth._save(str(web.app.secret_key), data)
    assert c.get("/api/integrations").get_json()["integrations"]["upstox"]["connected"] is False
    assert c.post("/api/integrations/upstox/disconnect", headers={"X-CSRF-Token": token}).status_code == 200


def test_indmoney_handoff_and_write_rejected(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    handoff = c.get("/integrations/indmoney/connect")
    assert handoff.status_code == 200
    assert "https://mcp.indmoney.com/mcp" in handoff.get_data(as_text=True)
    rejected = c.post("/api/integrations/indmoney/write", headers={"X-CSRF-Token": csrf(c)})
    assert rejected.status_code == 405
    assert c.get("/api/integrations").get_json()["integrations"]["indmoney"]["connected"] is False
