from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.risk import RiskEngine, RiskLimits
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderType, TimeInForce


NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def intent(key="risk-1", side="buy", qty="2", mode=ExecutionMode.PAPER, account="acct"):
    return OrderIntent(key, "p1", "INFY", side, OrderType.MARKET, TimeInForce.DAY, qty, "INR", mode, account_id=account)


def engine(tmp_path, limits):
    return RiskEngine(limits, db=Database(tmp_path / "risk.sqlite"), clock=lambda: NOW)


def test_allows_order_within_limits(tmp_path):
    result = engine(tmp_path, RiskLimits(max_order_notional="1000")).check(intent(), quote_price="100")
    assert result.allowed and result.code == "approved"


def test_rejects_max_order_notional(tmp_path):
    result = engine(tmp_path, RiskLimits(max_order_notional="199")).check(intent(), quote_price="100")
    assert not result.allowed and result.code == "max_order_notional"


def test_rejects_max_position_weight(tmp_path):
    limits = RiskLimits(max_position_weight="0.25")
    result = engine(tmp_path, limits).check(intent(), quote_price="100", portfolio_value="1000", current_position_notional="100")
    assert not result.allowed and result.code == "max_position_weight"


def test_rejects_stale_quote(tmp_path):
    limits = RiskLimits(max_quote_age=timedelta(minutes=5))
    result = engine(tmp_path, limits).check(intent(), quote_price="100", quote_timestamp=NOW - timedelta(minutes=6))
    assert not result.allowed and result.code == "stale_quote"


def test_market_and_session_gates_are_deterministic(tmp_path):
    result = engine(tmp_path, RiskLimits(require_market_open=True)).check(intent(), quote_price="100", market_open=False)
    assert result.code == "market_closed"
    result = engine(tmp_path, RiskLimits(require_session_allowed=True)).check(intent(), quote_price="100", session_allowed=False)
    assert result.code == "session_not_allowed"


def test_kill_switches_reject(tmp_path):
    assert engine(tmp_path, RiskLimits(global_kill_switch=True)).check(intent(), quote_price="100").code == "global_kill_switch"
    assert engine(tmp_path, RiskLimits(killed_accounts=frozenset({"acct"}))).check(intent(), quote_price="100").code == "account_kill_switch"


def test_duplicate_identical_intent_is_retry_but_conflict_rejects(tmp_path):
    db = Database(tmp_path / "risk.sqlite")
    db.init_db()
    db.insert_order_intent(intent().to_dict())
    risk = RiskEngine(db=db)
    retry = risk.check(intent(), quote_price="100")
    assert retry.allowed and retry.code == "idempotent_retry"
    conflict = risk.check(intent(qty="3"), quote_price="100")
    assert not conflict.allowed and conflict.code == "conflicting_idempotency"


def test_risk_recognizes_db_insert_as_identical_retry(tmp_path):
    db = Database(tmp_path / "risk.sqlite")
    db.init_db()
    db.insert_order_intent(intent().to_dict())
    decision = RiskEngine(db=db).check(intent(), quote_price="100")
    assert decision.allowed and decision.code == "idempotent_retry"


def test_rejects_float_inputs_in_new_risk_logic(tmp_path):
    with pytest.raises(TypeError):
        engine(tmp_path, RiskLimits(max_order_notional="1000")).check(intent(), quote_price=100.0)


def test_order_limit_includes_execution_slippage_and_fee(tmp_path):
    result = engine(tmp_path, RiskLimits(max_order_notional="204")).check(intent(), quote_price="100", fee_rate="0.01", slippage_bps="100")
    assert not result.allowed and result.code == "max_order_notional"


def test_idempotency_scope_includes_account_and_portfolio(tmp_path):
    db = Database(tmp_path / "risk.sqlite")
    db.init_db()
    db.insert_order_intent(intent().to_dict())
    assert engine(tmp_path, RiskLimits()).check(intent(account="other"), quote_price="100").code == "approved"
