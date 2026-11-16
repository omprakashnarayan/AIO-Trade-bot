from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.analytics import portfolio_analytics
from portfolio_tracker.db import Database
from portfolio_tracker.market_data import MarketDataService
from portfolio_tracker.ledger import FinancialEvent
from portfolio_tracker.reporting import portfolio_snapshot, render_markdown_report


def database(tmp_path):
    db = Database(tmp_path / "valuation.sqlite3")
    db.init_db()
    return db


def quote(price, currency="INR", *, age=timedelta(), as_of=None):
    return {"price": price, "currency": currency, "as_of": as_of or (datetime.now(timezone.utc) - age).isoformat(), "source": "fixture"}


def test_missing_price_is_not_zero_or_a_loss_in_snapshot(tmp_path, monkeypatch):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=2, avg_cost=10)
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {})
    section = portfolio_snapshot(db)["portfolios"][0]
    item = section["holdings"][0]
    assert item["market_value"] is item["gain"] is item["gain_pct"] is item["weight"] is None
    assert item["cost_basis"] == 20
    assert section["summary"]["total_value"] is section["summary"]["gain"] is None
    text = render_markdown_report({"generated_at": "now", "summary": {"portfolios": 1, "positions": 1}, "portfolios": [section]})
    assert "Total value: n/a" in text and "P/L n/a" in text
    assert "-20.00" not in text


@pytest.mark.parametrize("bad_quote", [quote(12, "USD"), quote(12, age=timedelta(days=10)), {"price": 12, "source": "unknown"}, quote(0)])
def test_unverified_quote_cannot_value_position_or_poison_cache(tmp_path, monkeypatch, bad_quote):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=2, avg_cost=10)
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {"ABC": bad_quote})
    item = portfolio_snapshot(db)["portfolios"][0]["holdings"][0]
    assert item["current_price"] is item["market_value"] is item["gain"] is None
    assert db.get_price_cache("Main", "ABC") is None


def test_stale_cache_cannot_be_reused_as_current_mark(tmp_path, monkeypatch):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10)
    db.set_price_cache("Main", "ABC", 5, (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), "old")
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {})
    item = portfolio_snapshot(db)["portfolios"][0]["holdings"][0]
    assert item["current_price"] is item["gain"] is None


def test_mixed_currency_section_has_no_unconverted_totals(tmp_path, monkeypatch):
    db = database(tmp_path)
    db.upsert_holding("A", shares=1, avg_cost=10, currency="USD")
    db.upsert_holding("B", shares=1, avg_cost=20, currency="INR")
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {"A": quote(12, "USD"), "B": quote(25)})
    section = portfolio_snapshot(db)["portfolios"][0]
    assert section["currency"] == "MIXED"
    assert section["summary"]["total_value"] is section["summary"]["total_cost"] is section["summary"]["gain"] is None
    assert {(h["symbol"], h["currency"], h["market_value"]) for h in section["holdings"]} == {("A", "USD", 12), ("B", "INR", 25)}
    assert all(h["weight"] is None for h in section["holdings"])
    text = render_markdown_report({"generated_at": "now", "summary": {"portfolios": 1, "positions": 2}, "portfolios": [section]})
    assert "Total value: n/a" in text and "$12.00" in text and "₹25.00" in text


def test_verified_same_currency_quotes_keep_prior_numeric_contract(tmp_path, monkeypatch):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=2, avg_cost=10)
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {"ABC": quote(12)})
    section = portfolio_snapshot(db)["portfolios"][0]
    assert section["summary"]["total_value"] == 24
    assert section["summary"]["gain"] == 4
    assert section["holdings"][0]["weight"] == 1


def test_saved_manual_quote_can_value_manual_holding_without_yfinance_currency_guess(tmp_path, monkeypatch):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=2, avg_cost=10, currency="INR")
    MarketDataService(db).manual_snapshot("ABC", "12", currency="INR")
    monkeypatch.setattr("portfolio_tracker.reporting.fetch_prices", lambda _: {})
    item = portfolio_snapshot(db)["portfolios"][0]["holdings"][0]
    assert item["current_price"] == 12
    assert item["source"] == "manual"
    assert item["market_value"] == 24


def test_analytics_rejects_quote_with_wrong_currency_or_stale_time(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=2, avg_cost=10)
    for price in (quote(12, "USD"), quote(12, age=timedelta(days=10))):
        result = portfolio_analytics(db, prices={"ABC": price})
        assert result["value"] is result["unrealized_pl"] is None
        assert result["positions"][0]["price"] is None


def test_analytics_same_symbol_different_currencies_requires_unambiguous_quotes(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10, currency="INR", portfolio="India")
    db.upsert_holding("ABC", shares=1, avg_cost=10, currency="USD", portfolio="US")
    result = portfolio_analytics(db, prices={"ABC": "12"}, rates={("USD", "INR"): "80"})
    assert result["value"] is None and result["unavailable_symbols"] == ["ABC"]
    result = portfolio_analytics(db, prices={("ABC", "INR"): quote(12), ("ABC", "USD"): quote(13, "USD")}, rates={("USD", "INR"): "80"})
    assert result["value"] == "1052"


def test_analytics_missing_fx_does_not_claim_total_cost_basis(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10, currency="USD")
    result = portfolio_analytics(db, prices={"ABC": quote(12, "USD")})
    assert result["value"] is result["cost_basis"] is result["unrealized_pl"] is None


def test_analytics_legacy_scalar_from_stale_or_wrong_currency_db_quote_is_unavailable(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    MarketDataService(db).manual_snapshot("ABC", "12", currency="INR", as_of=old)
    assert portfolio_analytics(db, prices={"ABC": "12"})["value"] is None
    MarketDataService(db).manual_snapshot("ABC", "13", currency="USD")
    assert portfolio_analytics(db, prices={"ABC": "13"})["value"] is None


def test_analytics_legacy_scalar_matching_fresh_db_quote_remains_valued(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10)
    MarketDataService(db).manual_snapshot("ABC", "12", currency="INR")
    assert portfolio_analytics(db, prices={"ABC": "12"})["value"] == "12"


def test_analytics_missing_filtered_source_never_falls_back_to_other_persisted_quote(tmp_path):
    db = database(tmp_path)
    db.upsert_holding("ABC", shares=1, avg_cost=10)
    MarketDataService(db).manual_snapshot("ABC", "12", currency="INR")
    result = portfolio_analytics(db, prices={})
    assert result["value"] is None and result["positions"][0]["price"] is None


def test_analytics_does_not_label_unconverted_realized_pl_as_base_currency(tmp_path):
    db = database(tmp_path)
    db.record_financial_event(FinancialEvent("buy", symbol="ABC", quantity=Decimal(1), price=Decimal(10), currency="USD", event_date="2026-01-01"))
    db.record_financial_event(FinancialEvent("sell", symbol="ABC", quantity=Decimal(1), price=Decimal(12), currency="USD", event_date="2026-01-02"))
    result = portfolio_analytics(db, base_currency="INR")
    assert result["realized_pl"] is None


def test_rated_same_currency_events_do_not_claim_unverified_fx_target(tmp_path):
    db = database(tmp_path)
    db.record_financial_event(FinancialEvent("buy", symbol="ABC", quantity=Decimal(1), price=Decimal(10), currency="USD", fx_rate=Decimal("80"), event_date="2026-01-01"))
    db.record_financial_event(FinancialEvent("sell", symbol="ABC", quantity=Decimal(1), price=Decimal(12), currency="USD", fx_rate=Decimal("80"), event_date="2026-01-02"))
    result = portfolio_analytics(db, base_currency="USD")
    assert result["realized_pl"] is None
