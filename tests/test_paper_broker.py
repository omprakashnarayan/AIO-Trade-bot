from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.paper_broker import LiveModeError, PaperBroker
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderType, TimeInForce


def intent(key="k1", side="buy", qty="2", mode=ExecutionMode.PAPER, account="acct", portfolio="p1"):
    return OrderIntent(key, portfolio, "INFY", side, OrderType.MARKET, TimeInForce.DAY, qty, "INR", mode, account_id=account)


def broker(tmp_path, **kwargs):
    b = PaperBroker(Database(tmp_path / "db.sqlite"), **kwargs)
    b.seed_cash("10000", currency="INR", account_id="acct", portfolio_id="p1")
    return b


def test_buy_sell_and_scope(tmp_path):
    b = broker(tmp_path)
    bought = b.submit(intent(), price="100")
    assert bought.status == "filled" and bought.fill.price == Decimal("100")
    sold = b.submit(intent("k2", "sell", "1"), price="110")
    assert sold.status == "filled"
    assert b.db.list_fills() and len(b.db.list_cash_ledger()) == 2
    assert b.db.list_holdings("p1")[0]["shares"] == 1.0


def test_rejects_cash_and_position(tmp_path):
    b = broker(tmp_path)
    result = b.submit(intent("cash", "buy", "200"), price="100")
    assert result.status == "rejected" and "cash" in result.error
    result = b.submit(intent("position", "sell", "1"), price="100")
    assert result.status == "rejected" and "position" in result.error
    assert not b.db.list_fills() and not b.db.list_cash_ledger()


def test_duplicate_is_idempotent(tmp_path):
    b = broker(tmp_path)
    first = b.submit(intent(), price="100")
    second = b.submit(intent(), price="999")
    assert second.reused and second.order_id == first.order_id
    assert len(b.db.list_fills()) == 1 and len(b.db.list_cash_ledger()) == 1


def test_fees_and_slippage(tmp_path):
    b = broker(tmp_path, fee_rate="0.01", slippage_bps="100")
    result = b.submit(intent(), price="100")
    assert result.fill.price == Decimal("101")
    assert result.total == Decimal("204.02")
    assert Decimal(b.db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="p1")["balance"]) == Decimal("9795.98")


def test_live_mode_rejected(tmp_path):
    b = broker(tmp_path)
    with pytest.raises(LiveModeError):
        b.submit(intent("live", mode=ExecutionMode.LIVE), price="100")
    assert b.db.list_order_intents() == []


def test_atomic_failure_rolls_back_all_state(tmp_path, monkeypatch):
    b = broker(tmp_path)
    monkeypatch.setattr(b, "_audit", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit failure")))
    with pytest.raises(RuntimeError):
        b.submit(intent(), price="100")
    assert b.db.list_order_intents() == []
    assert b.db.list_fills() == []
    assert b.db.list_cash_ledger() == []
    assert b.db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="p1")["balance"] == "10000"


def test_account_only_uses_empty_portfolio_scope(tmp_path):
    b = PaperBroker(Database(tmp_path / "db.sqlite"))
    b.seed_cash("1000", currency="INR", account_id="acct")
    result = b.submit(intent("account-only", qty="1", portfolio=None), price="100")
    assert result.filled
    assert b.db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="")["balance"] == "900.000000000000000000"
    assert b.db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="p1") is None
    assert b.db.get_paper_holding(portfolio="", symbol="INFY")["shares"] == "1"


def test_broker_identity_isolation(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    first = PaperBroker(db, broker_id="broker-a")
    second = PaperBroker(db, broker_id="broker-b")
    first.seed_cash("1000", currency="INR", account_id="acct", portfolio_id="p1")
    second.seed_cash("1000", currency="INR", account_id="acct", portfolio_id="p1")
    one = first.submit(intent("same"), price="100")
    two = second.submit(intent("same"), price="200")
    assert one.order_id != two.order_id
    assert not two.reused
    assert len(db.list_order_intents()) == 2


def test_broker_identity_isolates_cash_holdings_and_transactions(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    first = PaperBroker(db, broker_id="broker-a")
    second = PaperBroker(db, broker_id="broker-b")
    first.seed_cash("1000", currency="INR", account_id="acct", portfolio_id="p1")
    second.seed_cash("2000", currency="INR", account_id="acct", portfolio_id="p1")

    first.submit(intent("a"), price="100")
    second.submit(intent("b"), price="200")

    assert db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="p1", broker_id="broker-a")["balance"] == "800.000000000000000000"
    assert db.get_cash_balance(currency="INR", account_id="acct", portfolio_id="p1", broker_id="broker-b")["balance"] == "1600.000000000000000000"
    assert db.get_paper_holding(broker_id="broker-a", portfolio="p1", symbol="INFY")["shares"] == "2"
    assert db.get_paper_holding(broker_id="broker-b", portfolio="p1", symbol="INFY")["shares"] == "2"
    assert len(db.list_cash_ledger(broker_id="broker-a")) == 1
    assert len(db.list_cash_ledger(broker_id="broker-b")) == 1
    assert [row["broker_id"] for row in db.list_paper_transactions()] == ["broker-a", "broker-b"]


def test_retry_appends_audit_without_mutating_trade_state(tmp_path):
    b = broker(tmp_path)
    b.submit(intent(), price="100")
    before = (len(b.db.list_fills()), len(b.db.list_cash_ledger()), len(b.db.list_holdings()))
    b.submit(intent(), price="999")
    after = (len(b.db.list_fills()), len(b.db.list_cash_ledger()), len(b.db.list_holdings()))
    events = b.db.list_audit_events(entity_type="order_intent")
    assert before == after
    assert [event["event_type"] for event in events] == ["order.filled", "order.retry"]


def test_conflicting_retry_with_same_idempotency_key_is_rejected(tmp_path):
    b = broker(tmp_path)
    first = b.submit(intent(), price="100")
    before = (len(b.db.list_order_intents()), len(b.db.list_fills()), len(b.db.list_cash_ledger()), len(b.db.list_audit_events()))

    with pytest.raises(ValueError, match="conflicting order payload"):
        b.submit(intent(qty="1"), price="100")

    after = (len(b.db.list_order_intents()), len(b.db.list_fills()), len(b.db.list_cash_ledger()), len(b.db.list_audit_events()))
    assert after == before
    assert b.db.get_order_intent(order_id=first.order_id)["status"] == "filled"


def test_decimal_precision_is_kept_in_paper_state(tmp_path):
    b = broker(tmp_path)
    b.submit(intent(qty="0.123456789123456789"), price="100.123456789123456789")
    row = b.db.get_paper_holding(portfolio="p1", symbol="INFY")
    assert row["shares"] == "0.123456789123456789"
    assert row["avg_cost"] == "100.123456789123456789"


def test_decimal_precision_is_kept_in_paper_transactions(tmp_path):
    b = broker(tmp_path)
    b.submit(intent(qty="0.123456789123456789"), price="100.123456789123456789")
    row = b.db.list_paper_transactions(broker_id="paper")[0]
    assert row["shares"] == "0.123456789123456789"
    assert row["price"] == "100.123456789123456789"
    assert row["fees"] == "0.000000000000000000"


def test_max_idempotency_key_has_bounded_fill_id(tmp_path):
    b = broker(tmp_path)
    key = "k" * 128
    result = b.submit(intent(key), price="100")
    assert len(result.fill.fill_id) <= 128
    assert result.fill.fill_id == b.submit(intent(key), price="999").fill.fill_id


def test_invalid_slippage_multiplier_rejected(tmp_path):
    with pytest.raises(ValueError, match="slippage_bps"):
        PaperBroker(Database(tmp_path / "db.sqlite"), slippage_bps="10000")
