from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.trading_models import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
    TradeProposal,
)


def make_intent(**overrides):
    values = {
        "idempotency_key": "intent-001",
        "portfolio_id": "india-short-term",
        "symbol": "INFY",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.DAY,
        "quantity": Decimal("2"),
        "currency": "INR",
        "mode": ExecutionMode.PAPER,
        "limit_price": Decimal("1500.25"),
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_valid_long_only_equity_intent_is_immutable_and_deterministic():
    intent = make_intent()

    assert intent.quantity == Decimal("2")
    assert intent.to_dict() == {
        "account_id": None,
        "currency": "INR",
        "idempotency_key": "intent-001",
        "limit_price": "1500.25",
        "mode": "paper",
        "order_type": "limit",
        "portfolio_id": "india-short-term",
        "quantity": "2",
        "side": "buy",
        "stop_price": None,
        "symbol": "INFY",
        "time_in_force": "day",
    }
    with pytest.raises(AttributeError):
        intent.quantity = Decimal("3")


def test_trade_proposal_serializes_without_secrets():
    proposal = TradeProposal(
        proposal_id="proposal-001",
        intent=make_intent(),
        strategy="mean-reversion-v1",
        rationale="Test proposal",
        expires_at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert proposal.to_dict() == {
        "expires_at": "2026-11-02T12:00:00+00:00",
        "intent": make_intent().to_dict(),
        "proposal_id": "proposal-001",
        "rationale": "Test proposal",
        "strategy": "mean-reversion-v1",
    }
    assert "secret" not in repr(proposal).lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("idempotency_key", ""),
        ("portfolio_id", "  "),
        ("quantity", Decimal("0")),
        ("quantity", Decimal("-1")),
        ("currency", "EUR"),
        ("mode", "staging"),
        ("side", "short"),
        ("order_type", "iceberg"),
        ("time_in_force", "gtc"),
        ("limit_price", Decimal("0")),
        ("stop_price", Decimal("-1")),
    ],
)
def test_invalid_values_are_rejected(field, value):
    with pytest.raises((TypeError, ValueError)):
        make_intent(**{field: value})


def test_account_id_is_accepted_as_explicit_identifier():
    intent = make_intent(portfolio_id=None, account_id="kite-account")
    assert intent.to_dict()["account_id"] == "kite-account"
    assert intent.to_dict()["portfolio_id"] is None


def test_at_least_one_portfolio_or_account_identifier_is_required():
    with pytest.raises(ValueError):
        make_intent(portfolio_id=None, account_id=None)
