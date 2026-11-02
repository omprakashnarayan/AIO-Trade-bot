from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.trading_models import (
    AssetClass,
    ExecutionMode,
    Fill,
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
        "asset_class": "equity",
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


@pytest.mark.parametrize(
    "order_type,limit_price,stop_price",
    [
        (OrderType.MARKET, None, None),
        (OrderType.LIMIT, Decimal("10"), None),
        (OrderType.STOP, None, Decimal("10")),
        (OrderType.STOP_LIMIT, Decimal("10"), Decimal("9")),
    ],
)
def test_each_order_type_accepts_only_its_required_prices(order_type, limit_price, stop_price):
    intent = make_intent(order_type=order_type, limit_price=limit_price, stop_price=stop_price)
    assert intent.order_type is order_type


@pytest.mark.parametrize(
    "order_type,limit_price,stop_price",
    [
        (OrderType.MARKET, Decimal("10"), None),
        (OrderType.MARKET, None, Decimal("10")),
        (OrderType.LIMIT, None, None),
        (OrderType.LIMIT, Decimal("10"), Decimal("9")),
        (OrderType.STOP, None, None),
        (OrderType.STOP, Decimal("10"), Decimal("9")),
        (OrderType.STOP_LIMIT, Decimal("10"), None),
        (OrderType.STOP_LIMIT, None, Decimal("9")),
    ],
)
def test_invalid_order_type_price_combinations_are_rejected(order_type, limit_price, stop_price):
    with pytest.raises(ValueError):
        make_intent(order_type=order_type, limit_price=limit_price, stop_price=stop_price)


@pytest.mark.parametrize("field", ["idempotency_key", "portfolio_id", "account_id", "symbol"])
@pytest.mark.parametrize("value", ["", "  ", "bad\nvalue", "x" * 129])
def test_identifiers_are_nonempty_control_free_and_bounded(field, value):
    overrides = {field: value}
    if field == "portfolio_id":
        overrides["account_id"] = "account-1"
    if field == "account_id":
        overrides["portfolio_id"] = None
    with pytest.raises((TypeError, ValueError)):
        make_intent(**overrides)


def test_broker_symbols_with_dot_and_exchange_prefix_are_preserved():
    assert make_intent(symbol="brk.b").symbol == "BRK.B"
    assert make_intent(symbol="nse:reliance").symbol == "NSE:RELIANCE"


def test_asset_class_is_serialized():
    assert make_intent(asset_class=AssetClass.EQUITY).to_dict()["asset_class"] == "equity"


def test_fill_validates_and_serializes():
    fill = Fill(
        fill_id="fill-001",
        intent_idempotency_key="intent-001",
        quantity=Decimal("2"),
        price=Decimal("1500.25"),
        currency="inr",
        filled_at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc),
    )
    assert fill.to_dict() == {
        "currency": "INR",
        "fill_id": "fill-001",
        "filled_at": "2026-11-02T12:00:00+00:00",
        "intent_idempotency_key": "intent-001",
        "price": "1500.25",
        "quantity": "2",
    }
