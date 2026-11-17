"""Broker MCP data must be visible without merging it into manual/paper holdings."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re

from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database
from portfolio_tracker.provider_data import ProviderHolding, ProviderSnapshot
from portfolio_tracker import reporting
from portfolio_tracker.market_data import Quote


def _client(tmp_path, monkeypatch):
    db = Database(tmp_path / "provider-display.sqlite3")
    db.init_db()
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("private-test-password"))
    monkeypatch.setattr(web.integration_auth, "get_provider", lambda _secret, _name: {"account_id": "acct"})
    monkeypatch.setattr(web.integration_auth, "status", lambda _secret, name: {
        "connected": True, "status": "connected", "source": name, "capabilities": {"read": True, "reconciliation": True},
        "mcp_verified": True, "oauth_authorized": True, "discovered_tools": [], "read_only": name == "indmoney",
    })
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "private-test-password"}).status_code == 302
    return db, client


def _seed(db, name, symbol, currency, *, account="acct", source=None):
    captured = datetime.now(timezone.utc).isoformat()
    db.upsert_provider_snapshot(ProviderSnapshot(
        provider=name, broker_id=name, account_id=account, source=source or name, captured_at=captured,
        holdings=(ProviderHolding(symbol, Decimal("2.25"), Decimal("123.45"), currency, provider_id="linked-broker"),),
        positions=(), holdings_complete=True, positions_complete=True,
    ))
    run = db.create_provider_sync_run(name, account, name, started_at=captured)
    db.finish_provider_sync_run(run, status="succeeded", finished_at=captured, error=None, counts={"holdings": 1})


def test_dashboard_and_portfolio_api_show_scoped_broker_rows_without_fake_marks(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "kite", "INFY", "INR")
    _seed(db, "upstox", "INFY", "INR")  # Same ticker must not be collapsed across brokers.
    _seed(db, "indmoney", "AAPL", "USD")
    _seed(db, "kite", "SECRET", "INR", account="another-account")

    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "BROKER MCP HOLDINGS" in html
    assert html.count('data-broker="kite"') == 1
    assert html.count('data-broker="upstox"') == 1
    assert html.count('data-broker="indmoney"') == 1
    assert "AAPL" in html and "USD" in html and "linked-broker" in html
    assert "SECRET" not in html
    assert "Marks unavailable" in html
    assert "No local holdings" in html  # Manual holdings are still distinct.
    assert "initialBrokerHoldings" in html and "location.reload()" in html

    payload = client.get("/api/portfolio").get_json()
    initial = re.search(r'const initialBrokerHoldings=(\[.*?\]);const initialDashboardStatus=.*?;const commands=', html)
    assert initial and json.loads(initial.group(1)) == payload["broker_holdings"]
    assert payload["snapshot"]["summary"]["positions"] == 0
    assert payload["snapshot"]["portfolios"] == []
    groups = {group["broker_id"]: group for group in payload["broker_holdings"]}
    assert {name: len(group["holdings"]) for name, group in groups.items()} == {"kite": 1, "upstox": 1, "indmoney": 1}
    assert groups["indmoney"]["holdings"][0]["currency"] == "USD"
    assert all(group["read_only"] for group in groups.values())
    assert client.get("/api/holdings").get_json()["holdings"] == []


def test_indmoney_verified_snapshot_is_readable_but_write_stays_blocked(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "indmoney", "MSFT", "USD")
    response = client.get("/api/integrations/indmoney/snapshot")
    assert response.status_code == 200
    data = response.get_json()
    assert data["integration"] == "indmoney"
    assert len(data["holdings"]) == 1
    assert data["holdings"][0]["currency"] == "USD"
    with client.session_transaction() as sess:
        csrf = sess["csrf_token"]
    assert client.post("/api/integrations/indmoney/write", headers={"X-CSRF-Token": csrf}).status_code == 405
    fresh = web.app.test_client()
    assert fresh.get("/api/integrations/indmoney/snapshot").status_code == 401


def test_broker_snapshot_keeps_positions_separate_from_holdings(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    captured = datetime.now(timezone.utc).isoformat()
    db.upsert_provider_snapshot(ProviderSnapshot(
        provider="kite", broker_id="kite", account_id="acct", source="kite", captured_at=captured,
        holdings=(ProviderHolding("INFY", Decimal("2"), Decimal("100"), "INR"),),
        positions=(ProviderHolding("NIFTY-FUT", Decimal("-1"), Decimal("200"), "INR"),),
        holdings_complete=True, positions_complete=True,
    ))
    result = client.get("/api/integrations/kite/snapshot")
    assert result.status_code == 200
    assert [r["symbol"] for r in result.get_json()["holdings"]] == ["INFY"]
    assert [r["symbol"] for r in result.get_json()["positions"]] == ["NIFTY-FUT"]
    assert client.get("/api/holdings").get_json()["holdings"] == []


def test_failed_latest_sync_marks_cached_broker_rows_stale(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "kite", "RELIANCE", "INR")
    failed = db.create_provider_sync_run("kite", "acct", "kite", started_at=datetime.now(timezone.utc).isoformat())
    db.finish_provider_sync_run(failed, status="error", finished_at=datetime.now(timezone.utc).isoformat(),
                                error={"code": "mcp_timeout", "message": "read failed", "retryable": True}, counts={})
    html = client.get("/page/integrations").get_data(as_text=True)
    assert "● STALE" in html
    dashboard = client.get("/").get_data(as_text=True)
    assert "RELIANCE" in dashboard and "cached" in dashboard.lower()


def test_old_successful_sync_is_labeled_stale_with_last_sync_time(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    run = db.create_provider_sync_run("kite", "acct", "kite", started_at=old)
    db.finish_provider_sync_run(run, status="succeeded", finished_at=old, counts={"holdings": 0})
    dashboard = client.get("/").get_data(as_text=True)
    assert "KITE</span><b class=\"warn\">STALE" in dashboard
    assert old in dashboard


def test_integrations_page_links_to_human_readable_broker_holdings(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "indmoney", "NVDA", "USD")
    response = client.get("/page/integrations")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'href="/#broker-holdings-indmoney"' in html
    assert 'href="/api/integrations/indmoney/snapshot"' in html


def test_manual_value_is_unavailable_without_marks_or_verified_fx(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(reporting, "fetch_prices", lambda _symbols: {})
    db.upsert_holding("INFY", shares=1, avg_cost=100, portfolio="India", currency="INR")
    db.upsert_holding("AAPL", shares=1, avg_cost=100, portfolio="US", currency="USD")
    html = client.get("/").get_data(as_text=True)
    assert re.search(r'MANUAL VALUE</span><div class="price warn">UNAVAILABLE', html)
    assert re.search(r'MANUAL P/L</span><div class="price warn">UNAVAILABLE', html)


def test_analytics_page_resolves_loading_and_keeps_broker_scope_visible(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "kite", "INFY", "INR")
    html = client.get("/page/analytics").get_data(as_text=True)
    assert 'id="analytics-state"' in html
    assert "Loading analytics" not in html
    assert "KITE" in html and "1 holdings" in html
    assert 'href="/#broker-holdings-kite"' in html


def test_analytics_api_does_not_mark_with_wrong_currency_or_old_quote(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    db.upsert_holding("AAPL", shares=1, avg_cost=100, portfolio="US", currency="USD")
    db.save_market_quote(Quote("AAPL", Decimal("50"), "INR", datetime.now(timezone.utc).isoformat(), "manual"))
    result = client.get("/api/analytics/portfolio?currency=USD").get_json()
    assert result["value"] is None and "AAPL" in result["unavailable_symbols"]
    db.save_market_quote(Quote("AAPL", Decimal("99"), "USD", "2020-01-01T00:00:00+00:00", "manual"))
    result = client.get("/api/analytics/portfolio?currency=USD").get_json()
    assert result["value"] is None and "AAPL" in result["unavailable_symbols"]


def test_analytics_api_respects_configured_quote_freshness(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    db.upsert_holding("AAPL", shares=1, avg_cost=100, portfolio="US", currency="USD")
    db.save_market_quote(Quote("AAPL", Decimal("99"), "USD", (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(), "manual"))
    result = client.get("/api/analytics/portfolio?currency=USD").get_json()
    assert result["value"] is None and "AAPL" in result["unavailable_symbols"]


def test_get_views_do_not_overwrite_saved_daily_report(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    import portfolio_tracker.reporting as reporting_module
    report_dir = tmp_path / "reports"
    monkeypatch.setattr(reporting_module, "REPORTS_DIR", report_dir)
    for url in ("/", "/api/portfolio", "/api/report", "/refresh"):
        response = client.get(url)
        assert response.status_code == 200, url
    assert not report_dir.exists()


def test_mixed_manual_rows_render_native_currencies_without_a_total(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    db.upsert_holding("AAPL", shares=1, avg_cost=10, portfolio="Mixed", currency="USD")
    db.upsert_holding("INFY", shares=1, avg_cost=20, portfolio="Mixed", currency="INR")
    monkeypatch.setattr(reporting, "fetch_prices", lambda _symbols: {
        "AAPL": {"price": 12, "currency": "USD", "as_of": datetime.now(timezone.utc).isoformat()},
        "INFY": {"price": 25, "currency": "INR", "as_of": datetime.now(timezone.utc).isoformat()},
    })
    html = client.get("/").get_data(as_text=True)
    assert "$12.00" in html and "₹25.00" in html
    assert "MIXED 12.00" not in html and "MIXED 25.00" not in html
    assert re.search(r'MANUAL VALUE</span><div class="price warn">UNAVAILABLE', html)


def test_missing_one_currency_mark_keeps_other_currency_mark_for_same_symbol(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    db.upsert_holding("ABC", shares=1, avg_cost=10, portfolio="IN", currency="INR")
    db.upsert_holding("ABC", shares=1, avg_cost=10, portfolio="US", currency="USD")
    db.save_market_quote(Quote("ABC", Decimal("12"), "INR", datetime.now(timezone.utc).isoformat(), "manual"))
    result = client.get("/api/analytics/portfolio?currency=INR").get_json()
    values = {row["currency"]: row["price"] for row in result["positions"]}
    assert values == {"INR": "12", "USD": None}
    assert result["value"] is None


def test_dashboard_and_console_snapshot_explain_current_source_coverage(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    _seed(db, "kite", "INFY", "INR")
    _seed(db, "upstox", "TCS", "INR")
    _seed(db, "indmoney", "AAPL", "USD")

    payload = client.get("/api/console/snapshot").get_json()
    status = payload["data_status"]
    assert status["broker_totals"] == {"holdings": 3, "positions": 0, "cash": 0, "orders": 0, "fills": 0}
    workspaces = {item["key"]: item for item in status["workspaces"]}
    assert workspaces["manual_portfolio"]["count"] == 0
    assert workspaces["paper_trades"]["count"] == 0
    assert workspaces["ledger"]["count"] == 0
    assert workspaces["research"]["count"] == 0
    assert workspaces["finance"]["count"] == 0

    html = client.get("/").get_data(as_text=True)
    assert "DATA COVERAGE // CURRENT SOURCE STATUS" in html
    assert "Broker snapshots" in html and "3 holdings" in html
    assert "No provider orders or fills are stored in these snapshots" in html
    assert "Manual portfolio" in html and "Paper trades" in html
    assert "snapshot rows do not create trades or ledger entries" in html
    assert 'href="/page/stock-analysis"' in html and 'href="/page/finance"' in html
    assert "initialDashboardStatus" in html and "data_status" in html


def test_dashboard_shows_saved_goal_progress_and_finance_link(tmp_path, monkeypatch):
    db, client = _client(tmp_path, monkeypatch)
    from portfolio_tracker.finance_ops import create_goal, add_goal_contribution
    goal_id = create_goal(db, {"name": "Emergency fund", "target_amount": "100000", "starting_amount": "10000", "monthly_contribution": "0", "currency": "INR"})
    add_goal_contribution(db, goal_id, "9000", source="operator")
    html = client.get("/").get_data(as_text=True)
    assert "FINANCIAL GOALS" in html and "Emergency fund" in html
    assert "10%" in html and "19000" in html and "100000" in html and "INR" in html
