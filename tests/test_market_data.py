from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from portfolio_tracker.analytics import benchmark_comparison, max_drawdown, portfolio_analytics
from portfolio_tracker.db import Database
from portfolio_tracker.market_data import MarketDataService, ManualMarketTransport


def db(tmp_path: Path) -> Database:
    item = Database(tmp_path / "market.sqlite3"); item.init_db(); return item


def test_manual_source_is_explicit_and_durable(tmp_path):
    database = db(tmp_path); quote = MarketDataService(database).manual_snapshot("ABC", "12.50")
    assert quote.source == "manual"
    assert database.list_market_quotes(symbol="ABC")[0]["source"] == "manual"


def test_stale_quote_is_unavailable(tmp_path):
    database = db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    MarketDataService(database, stale_after_seconds=60).manual_snapshot("ABC", "10", as_of=old)
    status = MarketDataService(database, stale_after_seconds=60).status(["ABC"])["ABC"]
    assert status.stale and not status.available and status.reason == "stale quote"


def test_source_isolation_and_unconfigured_transport(tmp_path):
    database = db(tmp_path); service = MarketDataService(database, transports={"kite": ManualMarketTransport()})
    assert service.ingest_quotes("upstox", ["ABC"]) == []
    assert service.health()["configured_sources"] == ["kite"]


def test_injected_transport_quote_is_saved(tmp_path):
    database = db(tmp_path); transport = ManualMarketTransport(); transport.set_quote("ABC", "11")
    quotes = MarketDataService(database, {"manual": transport}).ingest_quotes("manual", ["ABC"])
    assert quotes[0].source == "manual" and database.list_market_quotes(symbol="ABC")[0]["price"] == "11"


def test_currency_conversion_and_decimal_analytics(tmp_path):
    database = db(tmp_path); database.upsert_holding("ABC", shares=2.0, avg_cost=10.0, currency="USD")
    result = portfolio_analytics(database, prices={"ABC": "12"}, rates={("USD", "INR"): "80"})
    assert result["value"] == "1920" and result["unrealized_pl"] == "320"
    assert result["concentration"]["currency"] == {"USD": "1"}


def test_missing_price_is_honest_unavailable(tmp_path):
    database = db(tmp_path); database.upsert_holding("ABC", shares=1, avg_cost=10)
    result = portfolio_analytics(database, prices={})
    assert not result["available"] and result["value"] is None and result["unavailable_symbols"] == ["ABC"]


def test_drawdown_and_benchmark_comparison_are_decimal(tmp_path):
    assert max_drawdown(["100", "80", "90"])["drawdown"] == "0.2"
    result = benchmark_comparison("100", "110", "100", "105", as_of="2026-01-02T00:00:00+00:00", benchmark_as_of="2026-01-02T00:00:00+00:00")
    assert result["excess_return"] == "0.05"
