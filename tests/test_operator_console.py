from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database


def auth_client(monkeypatch, db):
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return client


def test_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("PORTFOLIO_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("PORTFOLIO_ADMIN_PASSWORD_HASH", raising=False)
    client = web.app.test_client()
    assert client.get("/", follow_redirects=False).status_code == 302
    assert client.get("/api/risk").status_code == 503


def test_paper_order_uses_gateway_and_kill_switch(tmp_path, monkeypatch):
    db = Database(tmp_path / "console.sqlite3")
    client = auth_client(monkeypatch, db)
    db.init_db()
    db.upsert_cash_balance(balance="1000", currency="INR", account_id="acct", portfolio_id="Main", broker_id="paper")
    response = client.post("/api/orders/paper", json={"idempotency_key": "order-1", "account_id": "acct", "portfolio_id": "Main", "symbol": "INFY", "side": "buy", "quantity": "2", "quote_price": "100", "currency": "INR"})
    assert response.status_code == 200
    assert response.get_json()["order"]["status"] == "filled"
    assert client.post("/api/risk/kill-switch", json={"state": "on"}).status_code == 200
    blocked = client.post("/api/orders/paper", json={"idempotency_key": "order-2", "account_id": "acct", "portfolio_id": "Main", "symbol": "INFY", "side": "buy", "quantity": "1", "quote_price": "100", "currency": "INR"})
    assert blocked.status_code == 422
    assert blocked.get_json()["risk"]["code"] == "global_kill_switch"


def test_live_order_and_approval_never_execute(tmp_path, monkeypatch):
    client = auth_client(monkeypatch, Database(tmp_path / "console.sqlite3"))
    live = client.post("/api/orders/paper", json={"mode": "live", "idempotency_key": "live-1", "portfolio_id": "Main", "symbol": "INFY", "side": "buy", "quantity": "1", "quote_price": "100"})
    assert live.status_code == 409
    approval = client.post("/api/approvals", json={"proposal_id": "proposal-1", "proposal": {"proposal_id": "proposal-1", "mode": "live", "symbol": "INFY"}})
    assert approval.status_code == 201
    decided = client.post(f"/api/approvals/{approval.get_json()['id']}/decision", json={"status": "approved"})
    assert decided.status_code == 200
    assert decided.get_json()["execution"] == "still disabled"


def test_strategy_and_integration_surfaces_are_local(tmp_path, monkeypatch):
    client = auth_client(monkeypatch, Database(tmp_path / "console.sqlite3"))
    now = datetime.now(timezone.utc).isoformat()
    proposal = client.post("/api/strategies/proposals", json={"strategy": "kite-short-term/v1", "symbol": "INFY", "market": "INDIA", "currency": "INR", "price": "100", "as_of": now, "now": now, "source_id": "manual", "signal": "buy", "quantity": "1", "broker_id": "kite", "account_id": "acct", "portfolio_id": "Main"})
    assert proposal.status_code == 200
    assert proposal.get_json()["intent"]["mode"] == "paper"
    status = client.get("/api/integrations").get_json()["integrations"]
    assert status["indmoney"]["read_only"] is True
    assert "unconfigured" in status["kite"]["status"]
