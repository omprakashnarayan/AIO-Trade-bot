from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.execution import ExecutionGateway, LiveModeRejected
from portfolio_tracker.paper_broker import PaperBroker
from portfolio_tracker.risk import RiskEngine, RiskLimits
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderType, TimeInForce


def make_intent(key="exec-1", qty="2", mode=ExecutionMode.PAPER):
    return OrderIntent(key, "p1", "INFY", "buy", OrderType.MARKET, TimeInForce.DAY, qty, "INR", mode, account_id="acct")


def make_gateway(tmp_path, limits=None):
    db = Database(tmp_path / "execution.sqlite")
    broker = PaperBroker(db)
    broker.seed_cash("10000", currency="INR", account_id="acct", portfolio_id="p1")
    return ExecutionGateway(db, risk=RiskEngine(limits or RiskLimits(), db=db), paper_broker=broker), db


def test_passing_order_reaches_paper_broker_and_reports_decisions(tmp_path):
    gateway, db = make_gateway(tmp_path)
    result = gateway.submit(make_intent(), quote_price=Decimal("100"))
    assert result.status == "submitted"
    assert result.broker_result is not None and result.broker_result.filled
    assert [row["event_type"] for row in db.list_audit_events()] == ["execution.risk_approved", "order.filled", "execution.paper_result"]


def test_each_risk_rejection_stays_out_of_broker(tmp_path):
    gateway, db = make_gateway(tmp_path, RiskLimits(max_order_notional="100"))
    result = gateway.submit(make_intent(), quote_price=Decimal("100"))
    assert result.status == "rejected" and result.risk.code == "max_order_notional"
    assert db.list_fills() == []


def test_kill_switch_is_reported_and_blocks_execution(tmp_path):
    gateway, db = make_gateway(tmp_path, RiskLimits(global_kill_switch=True))
    result = gateway.submit(make_intent(), quote_price=Decimal("100"))
    assert result.risk.code == "global_kill_switch"
    assert db.list_order_intents() == []
    assert db.list_audit_events()[0]["event_type"] == "execution.risk_rejected"


def test_live_mode_fails_closed_without_creating_order(tmp_path):
    gateway, db = make_gateway(tmp_path)
    with pytest.raises(LiveModeRejected):
        gateway.submit(make_intent("live", mode=ExecutionMode.LIVE), quote_price=Decimal("100"))
    assert db.list_order_intents() == []
    assert db.list_audit_events()[0]["event_type"] == "execution.live_rejected"


def test_idempotent_retry_does_not_duplicate_paper_fill(tmp_path):
    gateway, db = make_gateway(tmp_path)
    first = gateway.submit(make_intent(), quote_price=Decimal("100"))
    second = gateway.submit(make_intent(), quote_price=Decimal("999"))
    assert first.broker_result is not None and second.broker_result is not None
    assert second.broker_result.reused
    assert len(db.list_fills()) == 1


def test_gateway_requires_validated_intent(tmp_path):
    gateway, _ = make_gateway(tmp_path)
    with pytest.raises(TypeError):
        gateway.submit({"idempotency_key": "bad"}, quote_price=Decimal("100"))
