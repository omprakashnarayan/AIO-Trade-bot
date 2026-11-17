from datetime import datetime, timedelta, timezone
from decimal import Decimal

from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database
from portfolio_tracker.provider_data import ProviderHolding, ProviderSnapshot
from portfolio_tracker.research import add_evidence


def _client(tmp_path, monkeypatch):
    db = Database(tmp_path / "stock-analysis.sqlite3")
    db.init_db()
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("test-password"))
    monkeypatch.setattr(web.integration_auth, "get_provider", lambda _secret, _name: {"account_id": "acct"})
    monkeypatch.setattr(web.integration_auth, "status", lambda _secret, name: {
        "connected": False, "status": "unconfigured", "source": name,
        "capabilities": {"read": True, "reconciliation": True},
        "mcp_verified": False, "oauth_authorized": False, "discovered_tools": [],
    })
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "test-password"}).status_code == 302
    return db, client


def _csrf(client):
    with client.session_transaction() as session:
        return {"X-CSRF-Token": session["csrf_token"]}


def test_stock_analysis_accepts_new_symbol_and_shows_source_separated_snapshots(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    db.upsert_holding("INFY", shares=2, avg_cost=100, portfolio="Manual", currency="INR")
    captured = datetime.now(timezone.utc).isoformat()
    db.upsert_provider_snapshot(ProviderSnapshot(
        provider="kite", broker_id="kite", account_id="acct", source="kite-mcp",
        captured_at=captured,
        holdings=(ProviderHolding("INFY", Decimal("3"), Decimal("120"), "INR"),),
        positions=(), holdings_complete=True, positions_complete=True,
    ))

    response = client.get("/page/stock-analysis?symbol=NEWCO")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "STOCK ANALYSIS" in html
    assert 'href="/page/stock-analysis"' in html and "aria-current=\"page\"" in html
    assert 'value="NEWCO"' in html
    assert "No manual holdings for this ticker" in html
    assert "No broker snapshot rows for this ticker" in html
    assert "UNAVAILABLE" in html
    assert "No current verified external evidence has been supplied" in html
    assert "No live quote, news, or fundamentals provider is configured" in html

    existing = client.get("/page/stock-analysis?symbol=INFY").get_data(as_text=True)
    assert "Manual" in existing and "2" in existing
    assert "KITE" in existing and "3" in existing
    assert "kite-mcp" in existing
    assert "MANUAL HOLDINGS SNAPSHOT" in existing and "BROKER MCP SNAPSHOTS" in existing
    assert "<h2>RECOMMENDATION" not in existing


def test_stock_analysis_run_uses_market_research_agent_and_keeps_empty_evidence_unavailable(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    response = client.post("/api/stock-analysis/run", json={"symbol": "NEWCO"})
    assert response.status_code == 403
    assert db.list_research_runs() == []

    response = client.post("/api/stock-analysis/run", json={"symbol": "NEWCO"}, headers=_csrf(client))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["agent"] == "market_research"
    assert payload["result"]["research_status"] == "unavailable"
    assert payload["result"]["external_call"] is False
    assert payload["result"]["findings"] == []
    assert db.list_research_runs()[-1]["topic"] == "stock_analysis:NEWCO"


def test_stock_analysis_run_uses_only_matching_verified_evidence(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    stamp = datetime.now(timezone.utc).isoformat()
    evidence_id = add_evidence(db, {
        "source": "filing", "source_identifier": "filing-1", "captured_at": stamp,
        "provider": "manual", "excerpt": "Filed revenue was reported as supplied in this excerpt.",
        "symbol": "ABC", "topic": "market", "verified": True,
    })
    add_evidence(db, {
        "source": "filing", "source_identifier": "filing-2", "captured_at": stamp,
        "provider": "manual", "excerpt": "This is unverified and must be rejected.",
        "symbol": "ABC", "topic": "market", "verified": False,
    })
    add_evidence(db, {
        "source": "filing", "source_identifier": "filing-3", "captured_at": stamp,
        "provider": "manual", "excerpt": "Evidence for a different ticker.",
        "symbol": "OTHER", "topic": "market", "verified": True,
    })
    add_evidence(db, {
        "source": "manual", "source_identifier": "unrelated-topic", "captured_at": stamp,
        "provider": "manual", "excerpt": "Same ticker but unrelated evidence topic.",
        "symbol": "ABC", "topic": "personal_finance", "verified": True,
    })
    db.create_research_evidence(
        source="filing", source_identifier="future", captured_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        provider="manual", excerpt="Future-dated fact must not be treated as current.",
        content_hash="future", symbol="ABC", topic="market", verified=True,
    )

    response = client.post("/api/stock-analysis/run", json={"symbol": "abc"}, headers=_csrf(client))
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["status"] == "succeeded"
    assert result["external_call"] is False
    assert len(result["findings"]) == 1
    assert result["rejected_fact_count"] == 2
    finding = result["findings"][0]
    assert finding["symbol"] == "ABC"
    assert finding["statement"] == "Filed revenue was reported as supplied in this excerpt."
    assert finding["evidence_ids"] == [evidence_id]
    assert db.list_research_runs()[-1]["topic"] == "stock_analysis:ABC"


def test_stock_analysis_rejects_invalid_tickers_without_running_agent(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    for symbol in ("", "BAD SYMBOL", "A/../B", "<script>"):
        response = client.post("/api/stock-analysis/run", json={"symbol": symbol}, headers=_csrf(client))
        assert response.status_code == 400
    assert db.list_research_runs() == []
    assert db.list_agent_runs("market_research") == []


def test_stock_analysis_rejects_non_object_json_body_without_running_agent(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    response = client.post("/api/stock-analysis/run", json=["malformed"], headers=_csrf(client))
    assert response.status_code == 400
    assert db.list_agent_runs("market_research") == []


def test_saved_stock_findings_are_labeled_historical(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    evidence_id = add_evidence(db, {
        "source": "filing", "source_identifier": "doc", "captured_at": datetime.now(timezone.utc).isoformat(),
        "excerpt": "Stored filing excerpt", "symbol": "INFY", "topic": "market", "verified": True,
    })
    from portfolio_tracker.research import run_research
    run_research(db, topic="stock_analysis:INFY", facts=[{"evidence_id": evidence_id}])
    response = client.get("/page/stock-analysis?symbol=INFY")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "HISTORICAL / LAST RUN" in html
    assert "Stored filing excerpt" in html


def test_stock_analysis_returns_error_status_when_agent_fails(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "run_agent", lambda *_args, **_kwargs: {
        "id": 91, "status": "failed", "result": {"status": "failed", "error": "agent failure"},
    })
    response = client.post("/api/stock-analysis/run", json={"symbol": "INFY"}, headers=_csrf(client))
    assert response.status_code == 502
    assert response.get_json()["status"] == "failed"
