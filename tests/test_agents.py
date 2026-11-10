from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database


def client_for(monkeypatch, tmp_path):
    db = Database(tmp_path / "agents.sqlite3")
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return client, db


def test_agent_list_run_test_retry_and_logs(tmp_path, monkeypatch):
    client, db = client_for(monkeypatch, tmp_path)
    listed = client.get("/api/agents")
    assert listed.status_code == 200
    assert {a["name"] for a in listed.get_json()["agents"]} == {
        "market_research", "kite_short_term_proposals", "upstox_long_term_proposals",
        "indmoney_us_import", "risk_review", "paper_trading", "scheduler",
    }
    test_run = client.post("/api/agents/risk_review/run", json={"test": True})
    assert test_run.status_code == 200
    assert test_run.get_json()["result"]["dry_run"] is True
    retry = client.post("/api/agents/risk_review/retry")
    assert retry.status_code == 200
    assert retry.get_json()["retry_of"] == test_run.get_json()["id"]
    logs = client.get("/api/agents/risk_review/runs")
    assert logs.status_code == 200
    assert len(logs.get_json()["runs"]) == 2


def test_disabled_unknown_and_validation(tmp_path, monkeypatch):
    client, _ = client_for(monkeypatch, tmp_path)
    assert client.patch("/api/agents/paper_trading", json={"enabled": False}).status_code == 200
    blocked = client.post("/api/agents/paper_trading/run")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "agent_disabled"
    assert client.patch("/api/agents/paper_trading", json={"enabled": "false"}).status_code == 400
    assert client.get("/api/agents/nope/runs").status_code == 404
    assert client.post("/api/agents/nope/run").status_code == 404


def test_agent_handlers_are_paper_safe(tmp_path, monkeypatch):
    client, db = client_for(monkeypatch, tmp_path)
    result = client.post("/api/agents/paper_trading/run")
    assert result.status_code == 200
    assert result.get_json()["result"]["paper_only"] is True
    assert result.get_json()["result"]["live_writes"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paper_transactions").fetchone()[0] == 0
    assert client.post("/api/agents/market_research/run").get_json()["result"]["status"] == "unconfigured_connector"
