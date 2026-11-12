from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database
from portfolio_tracker.research import add_evidence, run_research


def setup_client(monkeypatch, tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return client, db


def test_no_fabrication_and_stale_evidence(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    unavailable = run_research(db, topic="news", facts=[])
    assert unavailable["status"] == "unavailable"
    assert unavailable["external_call"] is False
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    evidence_id = add_evidence(db, {"source": "manual", "source_identifier": "doc-1", "captured_at": old, "provider": "manual", "excerpt": "old", "topic": "news"})
    stale = run_research(db, topic="news", facts=[{"evidence_id": evidence_id, "statement": "must not be used"}], max_age_seconds=60)
    assert stale["status"] == "unavailable"
    assert "stale" in stale["rejections"][0]


def test_provenance_feedback_and_paper_gate(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    evidence_id = add_evidence(db, {"source": "provider", "source_identifier": "url-1", "captured_at": datetime.now(timezone.utc).isoformat(), "provider": "kite", "excerpt": "verified fact", "symbol": "INFY", "topic": "market"})
    result = run_research(db, topic="market", facts=[{"evidence_id": evidence_id, "symbol": "INFY", "statement": "supplied fact"}])
    finding_id = result["findings"][0]["id"]
    assert result["findings"][0]["evidence_ids"] == [evidence_id]
    db.add_research_feedback(finding_id=finding_id, action="accepted", user_id="admin")
    assert db.list_research_findings()[0]["status"] == "accepted"


def test_authenticated_research_api_and_live_rejection(tmp_path, monkeypatch):
    client, db = setup_client(monkeypatch, tmp_path)
    assert client.get("/api/research/news").get_json()["external_call"] is False
    response = client.post("/api/research/run", json={"topic": "market", "facts": []})
    assert response.status_code == 200
    assert response.get_json()["status"] == "unavailable"
    live = client.post("/api/orders/paper", json={"mode": "live"})
    assert live.status_code == 409
    assert db.list_research_runs()
