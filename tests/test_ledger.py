from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.ledger import FinancialEvent, calculate, import_rows, record_event


def db(tmp_path):
    result = Database(path=tmp_path / "ledger.db")
    result.init_db()
    return result


def test_fifo_partial_sell_and_same_day_stable_order(tmp_path):
    database = db(tmp_path)
    record_event(database, FinancialEvent("buy", symbol="ABC", quantity=Decimal("10"), price=Decimal("10"), event_date="2024-01-01"))
    record_event(database, FinancialEvent("buy", symbol="ABC", quantity=Decimal("5"), price=Decimal("20"), event_date="2024-01-01"))
    record_event(database, FinancialEvent("sell", symbol="ABC", quantity=Decimal("12"), price=Decimal("30"), event_date="2025-01-01"))
    result = calculate(database)
    assert result["realized_pl"] == "220"
    assert result["lots"][0]["remaining_quantity"] == "3"
    assert result["matches"][0]["holding_period"] == "long"


def test_fees_dividend_fx_and_split(tmp_path):
    database = db(tmp_path)
    record_event(database, FinancialEvent("buy", symbol="ABC", quantity=Decimal("2"), price=Decimal("10"), fees=Decimal("1"), event_date="2025-01-01", fx_rate=Decimal("80"), fx_source="official", fx_as_of="2025-01-01"))
    record_event(database, FinancialEvent("split", symbol="ABC", quantity=Decimal("2"), price=Decimal("2"), event_date="2025-01-02"))
    record_event(database, FinancialEvent("dividend", symbol="ABC", gross_amount=Decimal("5"), currency="USD", fx_rate=Decimal("80"), fx_source="provider", fx_as_of="2025-01-03", event_date="2025-01-03"))
    result = calculate(database, market_prices={"ABC": "12"})
    assert result["income"] == "400"
    assert result["unrealized_pl"] == "2160.00"


def test_idempotent_import_invalid_rows_and_namespace_isolation(tmp_path):
    database = db(tmp_path)
    row = {"event_type": "buy", "symbol": "ABC", "quantity": "1", "price": "10", "event_date": "2025-01-01", "external_id": "x"}
    assert import_rows(database, [row], source="broker", namespace="broker")["accepted"] == 1
    assert import_rows(database, [row], source="broker", namespace="broker")["accepted"] == 1
    bad = import_rows(database, [{"event_type": "buy", "symbol": "ABC", "quantity": "nope", "price": "10"}])
    assert bad["rejected"] == 1
    assert len(database.list_financial_events(namespace="broker")) == 1
    assert len(database.list_financial_events(namespace="paper")) == 0


def test_sell_cannot_exceed_fifo_lots(tmp_path):
    database = db(tmp_path)
    record_event(database, FinancialEvent("buy", symbol="ABC", quantity=Decimal("1"), price=Decimal("10")))
    record_event(database, FinancialEvent("sell", symbol="ABC", quantity=Decimal("2"), price=Decimal("12")))
    with pytest.raises(ValueError, match="exceeds"):
        calculate(database)
