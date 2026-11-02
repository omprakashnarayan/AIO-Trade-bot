"""Immutable, broker-neutral trading domain values.

These models describe intent only. They do not contain credentials and do not
perform broker or market-data calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
import unicodedata


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ExecutionMode(_ValueEnum):
    PAPER = "paper"
    LIVE = "live"


# Friendly aliases for callers that use trading terminology.
TradingMode = ExecutionMode


class AssetClass(_ValueEnum):
    EQUITY = "equity"


class OrderSide(_ValueEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(_ValueEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(_ValueEnum):
    DAY = "day"
    IOC = "ioc"


_ALLOWED_CURRENCIES = frozenset({"USD", "INR"})
_MAX_IDENTIFIER_LENGTH = 128
_MAX_STRATEGY_LENGTH = 128
_MAX_RATIONALE_LENGTH = 4096
_MAX_DECIMAL_DIGITS = 38
_MAX_DECIMAL_EXPONENT = 18
_MAX_DECIMAL_SERIALIZED_LENGTH = 64


def _required_text(value: str, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"} or category.startswith("Z") and character != " ":
            raise ValueError(f"{field_name} must not contain control, format, or Unicode separator characters")
    if value.startswith(" ") or value.endswith(" "):
        raise ValueError(f"{field_name} must not contain leading or trailing spaces")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return value


def _identifier(value: str, field_name: str, *, allow_edge_ascii_spaces: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"} or category.startswith("Z") and character != " ":
            raise ValueError(f"{field_name} must not contain control, format, or separator characters")
    if value.startswith(" ") or value.endswith(" "):
        raise ValueError(f"{field_name} must not contain leading or trailing spaces")
    result = value
    if not result:
        raise ValueError(f"{field_name} must be non-empty")
    if " " in result:
        raise ValueError(f"{field_name} must not contain spaces")
    if len(result) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} must be at most {_MAX_IDENTIFIER_LENGTH} characters")
    return result


def _symbol(value: str, field_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper() if isinstance(value, str) else value
    return _identifier(normalized, field_name, allow_edge_ascii_spaces=False)


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, max_length=_MAX_RATIONALE_LENGTH)


def _optional_identifier(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _decimal(value: Decimal | int | str, field_name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a Decimal-compatible number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TypeError(f"{field_name} must be a Decimal-compatible number") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    sign, digits, exponent = result.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_DECIMAL_EXPONENT:
        raise ValueError(f"{field_name} is outside the supported numeric range")
    if len(format(result, "f")) > _MAX_DECIMAL_SERIALIZED_LENGTH:
        raise ValueError(f"{field_name} serialization is too large")
    if positive and result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}") from exc


def _iso_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A validated long-only equity order request."""

    idempotency_key: str
    portfolio_id: str | None
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal | int | str
    currency: str
    mode: ExecutionMode
    account_id: str | None = None
    limit_price: Decimal | int | str | None = None
    stop_price: Decimal | int | str | None = None
    asset_class: AssetClass = AssetClass.EQUITY
    broker_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "idempotency_key", _identifier(self.idempotency_key, "idempotency_key"))
        portfolio_id = _optional_identifier(self.portfolio_id, "portfolio_id")
        account_id = _optional_identifier(self.account_id, "account_id")
        broker_id = _optional_identifier(self.broker_id, "broker_id")
        if portfolio_id is None and account_id is None:
            raise ValueError("portfolio_id or account_id is required")
        object.__setattr__(self, "portfolio_id", portfolio_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "broker_id", broker_id)
        symbol = _symbol(self.symbol, "symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", _enum(self.side, OrderSide, "side"))
        object.__setattr__(self, "order_type", _enum(self.order_type, OrderType, "order_type"))
        object.__setattr__(self, "time_in_force", _enum(self.time_in_force, TimeInForce, "time_in_force"))
        object.__setattr__(self, "mode", _enum(self.mode, ExecutionMode, "mode"))
        object.__setattr__(self, "asset_class", _enum(self.asset_class, AssetClass, "asset_class"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        if not isinstance(self.currency, str) or self.currency.upper() not in _ALLOWED_CURRENCIES:
            raise ValueError("currency must be the ISO code USD or INR")
        object.__setattr__(self, "currency", self.currency.upper())
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", _decimal(self.limit_price, "limit_price", positive=True))
        if self.stop_price is not None:
            object.__setattr__(self, "stop_price", _decimal(self.stop_price, "stop_price", positive=True))
        has_limit = self.limit_price is not None
        has_stop = self.stop_price is not None
        required_prices = {
            OrderType.MARKET: (False, False),
            OrderType.LIMIT: (True, False),
            OrderType.STOP: (False, True),
            OrderType.STOP_LIMIT: (True, True),
        }[self.order_type]
        if (has_limit, has_stop) != required_prices:
            raise ValueError(f"price fields do not match order_type {self.order_type.value}")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "account_id": self.account_id,
            "asset_class": self.asset_class.value,
            "currency": self.currency,
            "idempotency_key": self.idempotency_key,
            "limit_price": _string_decimal(self.limit_price),
            "mode": self.mode.value,
            "order_type": self.order_type.value,
            "portfolio_id": self.portfolio_id,
            "quantity": _string_decimal(self.quantity),
            "side": self.side.value,
            "stop_price": _string_decimal(self.stop_price),
            "symbol": self.symbol,
            "time_in_force": self.time_in_force.value,
        }
        if self.broker_id is not None:
            result["broker_id"] = self.broker_id
        return result


def _string_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@dataclass(frozen=True, slots=True)
class TradeProposal:
    proposal_id: str
    intent: OrderIntent
    strategy: str
    rationale: str
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", _identifier(self.proposal_id, "proposal_id"))
        if not isinstance(self.intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        object.__setattr__(self, "strategy", _required_text(self.strategy, "strategy", max_length=_MAX_STRATEGY_LENGTH))
        object.__setattr__(self, "rationale", _required_text(self.rationale, "rationale", max_length=_MAX_RATIONALE_LENGTH))
        object.__setattr__(self, "expires_at", _iso_datetime(self.expires_at, "expires_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "expires_at": self.expires_at.isoformat(),
            "intent": self.intent.to_dict(),
            "proposal_id": self.proposal_id,
            "rationale": self.rationale,
            "strategy": self.strategy,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    intent_idempotency_key: str
    quantity: Decimal | int | str
    price: Decimal | int | str
    currency: str
    filled_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _identifier(self.fill_id, "fill_id"))
        object.__setattr__(self, "intent_idempotency_key", _identifier(self.intent_idempotency_key, "intent_idempotency_key"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", _decimal(self.price, "price", positive=True))
        if not isinstance(self.currency, str) or self.currency.upper() not in _ALLOWED_CURRENCIES:
            raise ValueError("currency must be the ISO code USD or INR")
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "filled_at", _iso_datetime(self.filled_at, "filled_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "fill_id": self.fill_id,
            "filled_at": self.filled_at.isoformat(),
            "intent_idempotency_key": self.intent_idempotency_key,
            "price": _string_decimal(self.price),
            "quantity": _string_decimal(self.quantity),
        }
