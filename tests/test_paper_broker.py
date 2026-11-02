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
