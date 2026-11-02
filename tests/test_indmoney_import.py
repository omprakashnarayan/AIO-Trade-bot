import json
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.imports.indmoney import INDMONEY_US_PORTFOLIO, import_indmoney_csv, import_indmoney_records


CSV = """symbol,shares,average_cost,currency,label,account,portfolio,source,imported_at
AAPL,2.5,180.25,USD,Apple,us-acct,INDmoney-US,statement,2026-11-02T12:00:00+00:00
MSFT,1,400,USD,Microsoft,us-acct,INDmoney-US,statement,2026-11-02T12:00:00+00:00
"""


def test_csv_import_is_local_only_idempotent_and_preserves_provenance(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    first = import_indmoney_csv(db, CSV)
    second = import_indmoney_csv(db, CSV)

    assert (first.imported, first.skipped) == (2, 0)
    assert (second.imported, second.skipped) == (0, 2)
    assert db.list_holdings(INDMONEY_US_PORTFOLIO) == []
    with db.connect() as conn:
        rows = conn.execute("SELECT symbol, shares, avg_cost, currency FROM indmoney_holdings WHERE portfolio=? ORDER BY symbol", (INDMONEY_US_PORTFOLIO,)).fetchall()
    assert [(r["symbol"], r["shares"], r["avg_cost"], r["currency"]) for r in rows] == [("AAPL", "2.5", "180.25", "USD"), ("MSFT", "1", "400", "USD")]
    assert len(db.list_audit_events(entity_type="indmoney_import_row")) == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM indmoney_import_rows").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM holdings WHERE symbol='AAPL'").fetchone()[0] == 0


def test_manual_import_does_not_touch_unrelated_portfolios(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.init_db()
    db.upsert_holding("INFY", shares=3, avg_cost=100, portfolio="India", currency="INR")

    import_indmoney_records(db, [{"symbol": "TSLA", "shares": "1", "average_cost": "250"}])

    assert db.list_holdings("India")[0]["symbol"] == "INFY"
    with db.connect() as conn:
        assert conn.execute("SELECT symbol FROM indmoney_holdings WHERE portfolio=?", (INDMONEY_US_PORTFOLIO,)).fetchone()["symbol"] == "TSLA"


@pytest.mark.parametrize("record", [
    {"symbol": "AAPL", "shares": "-1", "average_cost": "1"},
    {"symbol": "AAPL", "shares": "1", "average_cost": "NaN"},
    {"symbol": "AAPL", "shares": "1", "average_cost": "1", "currency": "INR"},
    {"symbol": "AAPL!", "shares": "1", "average_cost": "1"},
    {"symbol": "AAPL", "shares": "1"},
])
def test_invalid_records_are_rejected(tmp_path, record):
    with pytest.raises((ValueError, TypeError)):
        import_indmoney_records(Database(tmp_path / "portfolio.sqlite3"), [record])


def test_conflicting_duplicate_rows_are_rejected_atomically(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    records = [
        {"symbol": "AAPL", "shares": "1", "average_cost": "100"},
        {"symbol": "AAPL", "shares": "2", "average_cost": "100"},
    ]
    with pytest.raises(ValueError, match="conflicting duplicate"):
        import_indmoney_records(db, records)
    db.init_db()
    assert db.list_holdings() == []


def test_validation_precision_order_independence_and_provenance(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    records = [
        {"symbol": "AAPL", "shares": "0.123456789012345678", "average_cost": "180.250000000000000001", "imported_at": "2026-11-02T12:00:00+00:00", "source": "file"},
        {"symbol": "MSFT", "shares": "1", "average_cost": "400", "imported_at": "2026-11-02T12:00:00+00:00"},
    ]
    first = import_indmoney_records(db, records, source="explicit-source")
    second = import_indmoney_records(db, list(reversed(records)), source="explicit-source")
    assert (first.imported, second.skipped) == (2, 2)
    with db.connect() as conn:
        row = conn.execute("SELECT shares, avg_cost, source, source_imported_at, ingested_at FROM indmoney_holdings WHERE symbol='AAPL'").fetchone()
        assert row["shares"] == "0.123456789012345678"
        assert row["avg_cost"] == "180.250000000000000001"
        assert row["source"] == "explicit-source"
        assert row["source_imported_at"].endswith("+00:00")
        assert row["ingested_at"] and row["ingested_at"] != row["source_imported_at"]


def test_timezone_and_namespace_validation(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    with pytest.raises(ValueError, match="timezone"):
        import_indmoney_records(db, [{"symbol": "AAPL", "shares": "1", "average_cost": "1", "imported_at": "2026-11-02T12:00:00"}])
    with pytest.raises(ValueError, match="dedicated"):
        import_indmoney_records(db, [{"symbol": "AAPL", "shares": "1", "average_cost": "1", "portfolio": "India"}])


@pytest.mark.parametrize("portfolio", ["INDmoney US", "INDmoney-US:taxable", "Main", "India", "other"])
def test_only_exact_indmoney_us_portfolio_is_accepted(tmp_path, portfolio):
    with pytest.raises(ValueError, match="dedicated"):
        import_indmoney_records(Database(tmp_path / "portfolio.sqlite3"), [{"symbol": "AAPL", "shares": "1", "average_cost": "1", "portfolio": portfolio}])


def test_import_portfolio_override_cannot_create_an_alternate_namespace(tmp_path):
    with pytest.raises(ValueError, match="dedicated"):
        import_indmoney_records(Database(tmp_path / "portfolio.sqlite3"), [{"symbol": "AAPL", "shares": "1", "average_cost": "1"}], portfolio="INDmoney-US:broker")


def test_malformed_csv_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        import_indmoney_csv(Database(tmp_path / "portfolio.sqlite3"), "symbol,shares,average_cost\n\"AAPL,1,100\n")


def test_duplicate_headers_after_alias_normalization_are_rejected():
    with pytest.raises(ValueError, match="alias normalization"):
        from portfolio_tracker.imports.indmoney import parse_indmoney_csv
        parse_indmoney_csv("symbol,ticker,shares,average_cost\nAAPL,AAPL,1,1\n")
