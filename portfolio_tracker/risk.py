"""Deterministic pre-trade risk checks with no broker or network access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .db import Database
from .idempotency import canonical_idempotency_key
from .trading_models import OrderIntent


def _decimal(value: Decimal | int | str | None, name: str, *, nonnegative: bool = False) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must be a Decimal, integer, or string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal | int | str | None = None
    max_position_weight: Decimal | int | str | None = None
    max_quote_age: timedelta | None = None
    require_market_open: bool = False
    require_session_allowed: bool = False
    global_kill_switch: bool = False
    killed_accounts: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        max_order = _decimal(self.max_order_notional, "max_order_notional", nonnegative=True)
        max_weight = _decimal(self.max_position_weight, "max_position_weight", nonnegative=True)
        if max_weight is not None and max_weight > 1:
            raise ValueError("max_position_weight must be between 0 and 1")
        if self.max_quote_age is not None and (not isinstance(self.max_quote_age, timedelta) or self.max_quote_age < timedelta(0)):
            raise ValueError("max_quote_age must be a nonnegative timedelta")
        if not isinstance(self.killed_accounts, (frozenset, set, tuple)):
            raise TypeError("killed_accounts must be a set-like collection")
        object.__setattr__(self, "max_order_notional", max_order)
        object.__setattr__(self, "max_position_weight", max_weight)
        object.__setattr__(self, "killed_accounts", frozenset(self.killed_accounts))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str
    details: dict[str, Any]


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None, *, db: Database | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.db = db
        if self.db is not None:
            self.db.init_db()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def check(
        self,
        intent: OrderIntent,
        *,
        quote_price: Decimal | int | str,
        quote_timestamp: datetime | None = None,
        portfolio_value: Decimal | int | str | None = None,
        current_position_notional: Decimal | int | str = Decimal("0"),
        market_open: bool | None = None,
        session_allowed: bool | None = None,
        fee_rate: Decimal | int | str = Decimal("0"),
        slippage_bps: Decimal | int | str = Decimal("0"),
        broker_id: str = "paper",
    ) -> RiskDecision:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        price = _decimal(quote_price, "quote_price", nonnegative=True)
        assert price is not None
        if price <= 0:
            return self._reject("invalid_quote", "quote_price must be positive", {"quote_price": str(price)})
        if self.limits.global_kill_switch:
            return self._reject("global_kill_switch", "global kill switch is active", {})
        if intent.account_id in self.limits.killed_accounts:
            return self._reject("account_kill_switch", "account kill switch is active", {"account_id": intent.account_id})
        if market_open is False or (self.limits.require_market_open and market_open is not True):
            return self._reject("market_closed", "market/session gate is closed", {"market_open": market_open})
        if session_allowed is False or (self.limits.require_session_allowed and session_allowed is not True):
            return self._reject("session_not_allowed", "market/session gate is not allowed", {"session_allowed": session_allowed})
        if self.limits.max_quote_age is not None:
            if quote_timestamp is None:
                return self._reject("stale_quote", "quote timestamp is required", {})
            timestamp = _timestamp(quote_timestamp, "quote_timestamp")
            age = self._clock() - timestamp
            if age < timedelta(0) or age > self.limits.max_quote_age:
                return self._reject("stale_quote", "quote is stale", {"age_seconds": str(age.total_seconds())})
        fee = _decimal(fee_rate, "fee_rate", nonnegative=True)
        slippage = _decimal(slippage_bps, "slippage_bps", nonnegative=True)
        assert fee is not None and slippage is not None
        execution_price = price * (Decimal("1") + slippage / Decimal("10000") * (Decimal("1") if str(intent.side) == "buy" else Decimal("-1")))
        notional = execution_price * intent.quantity
        execution_exposure = notional * (Decimal("1") + fee if str(intent.side) == "buy" else Decimal("1") - fee)
        if self.limits.max_order_notional is not None and execution_exposure > self.limits.max_order_notional:
            return self._reject("max_order_notional", "executed order exposure exceeds limit", {"exposure": str(execution_exposure), "limit": str(self.limits.max_order_notional)})
        if self.limits.max_position_weight is not None:
            if portfolio_value is None:
                return self._reject("missing_portfolio_value", "portfolio value is required for position-weight check", {})
            value = _decimal(portfolio_value, "portfolio_value", nonnegative=True)
            current = _decimal(current_position_notional, "current_position_notional", nonnegative=True)
            assert value is not None and current is not None
            if value <= 0:
                return self._reject("invalid_portfolio_value", "portfolio_value must be positive", {})
            projected = current + notional if str(intent.side) == "buy" else max(Decimal("0"), current - notional)
            weight = projected / value
            if weight > self.limits.max_position_weight:
                return self._reject("max_position_weight", "projected position weight exceeds limit", {"weight": str(weight), "limit": str(self.limits.max_position_weight)})
        duplicate = self._existing_intent(intent, broker_id=broker_id)
        if duplicate is not None:
            payload = json.loads(duplicate["order_payload"])
            if payload != intent.to_dict():
                return self._reject("conflicting_idempotency", "idempotency key conflicts with an existing intent", {})
            return RiskDecision(True, "idempotent_retry", "identical idempotency key may be retried", {"order_id": duplicate["id"]})
        return RiskDecision(True, "approved", "risk checks passed", {"notional": str(notional)})

    def _existing_intent(self, intent: OrderIntent, *, broker_id: str = "paper"):
        if self.db is None:
            return None
        for row in self.db.list_order_intents():
            try:
                payload = json.loads(row["order_payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            expected_key = canonical_idempotency_key(broker_id, intent.account_id, intent.portfolio_id, intent.idempotency_key)
            if (row["canonical_idempotency_key"] == expected_key and row["broker_id"] == broker_id
                    and row["account_id"] == intent.account_id and row["portfolio_id"] == intent.portfolio_id):
                return row
        return None

    @staticmethod
    def _reject(code: str, message: str, details: dict[str, Any]) -> RiskDecision:
        return RiskDecision(False, code, message, details)
