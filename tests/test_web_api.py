from portfolio_tracker import web
from portfolio_tracker.db import Database
from werkzeug.security import generate_password_hash


def _authenticated_client(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("test-password"))
    monkeypatch.setenv("PORTFOLIO_SESSION_SECRET", "test-session-secret")
    client = web.app.test_client()
    response = client.post("/login", data={"username": "admin", "password": "test-password"})
    assert response.status_code == 302
    return client


def test_local_holdings_crud_and_zero_share_delete(tmp_path, monkeypatch):
    db = Database(tmp_path / "portfolio.sqlite3")
    monkeypatch.setattr(web, "Database", lambda: db)
    client = _authenticated_client(monkeypatch)

    created = client.post("/api/holdings", json={"portfolio": "Main", "symbol": "INFY", "shares": 2, "avg_cost": 100, "label": "Infosys"})
    assert created.status_code == 201
    assert created.get_json()["symbol"] == "INFY"

    blocked = client.delete("/api/holdings/Main/INFY")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "non_zero_shares"

    updated = client.put("/api/holdings/Main/INFY", json={"shares": 0, "avg_cost": 100, "notes": "closed"})
    assert updated.status_code == 200
    assert updated.get_json()["shares"] == 0

    deleted = client.delete("/api/holdings/Main/INFY")
    assert deleted.status_code == 200
    assert client.get("/api/holdings").get_json()["holdings"] == []


def test_settings_api_is_local_and_rejects_unknown_risk(tmp_path, monkeypatch):
    settings = {"owner_name": "", "risk_profile": "balanced", "monthly_contribution": "", "reminder_time": "08:00", "notes": ""}
    monkeypatch.setattr(web, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(web, "save_settings", lambda value: settings.update(value))
    client = _authenticated_client(monkeypatch)

    response = client.patch("/api/settings", json={"owner_name": "NOC", "risk_profile": "low"})
    assert response.status_code == 200
    assert response.get_json()["owner_name"] == "NOC"
    assert response.get_json()["risk_profile"] == "low"

    invalid = client.put("/api/settings", json={"risk_profile": "live"})
    assert invalid.status_code == 400
    assert "risk_profile" in invalid.get_json()["error"]
