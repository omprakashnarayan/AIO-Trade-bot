import json
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.imports.indmoney import import_indmoney_csv, import_indmoney_records


CSV = """symbol,shares,average_cost,currency,label,account,portfolio,source,imported_at
AAPL,2.5,180.25,USD,Apple,us-acct,INDmoney US,statement,2026-11-02T12:00:00+00:00
MSFT,1,400,USD,Microsoft,us-acct,INDmoney US,statement,2026-11-02T12:00:00+00:00
"""


def test_csv_import_is_local_only_idempotent_and_preserves_provenance(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    first = import_indmoney_csv(db, CSV)
    second = import_indmoney_csv(db, CSV)

    assert (first.imported, first.skipped) == (2, 0)
    assert (second.imported, second.skipped) == (0, 2)
    rows = db.list_holdings("INDmoney US")
    assert [(r["symbol"], r["shares"], r["currency"]) for r in rows] == [("AAPL", 2.5, "USD"), ("MSFT", 1.0, "USD")]
    assert len(db.list_audit_events(entity_type="indmoney_import_row")) == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM indmoney_import_rows").fetchone()[0] == 2
        assert "statement" in conn.execute("SELECT notes FROM holdings WHERE symbol='AAPL'").fetchone()[0]


def test_manual_import_does_not_touch_unrelated_portfolios(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.init_db()
    db.upsert_holding("INFY", shares=3, avg_cost=100, portfolio="India", currency="INR")

    import_indmoney_records(db, [{"symbol": "TSLA", "shares": "1", "average_cost": "250"}])

    assert db.list_holdings("India")[0]["symbol"] == "INFY"
    assert db.list_holdings("INDmoney US")[0]["symbol"] == "TSLA"


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


def test_malformed_csv_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        import_indmoney_csv(Database(tmp_path / "portfolio.sqlite3"), "symbol,shares,average_cost\n\"AAPL,1,100\n")
