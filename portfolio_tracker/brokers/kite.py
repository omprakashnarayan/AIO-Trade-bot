"""Kite Connect adapter boundary; live writes require an internal gateway capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import re
from typing import Any, Mapping

from .base import (
    AdapterCapability, AdapterConfigurationError, BrokerAdapterError, ConfiguredAdapter,
    LiveExecutionDisabled, OrderReceipt, Position, Quote, AccountSnapshot, ReconciliationReport,
    TokenExpiryMetadata, normalize_status, require_intent, _ExecutionCapability,
)
from ..trading_models import OrderIntent, OrderType


@dataclass(frozen=True, slots=True)
class KiteConfig:
    account_id: str
    api_key: str = field(repr=False)
    access_token: str | None = field(default=None, repr=False)
    broker_id: str = "kite"
    live_enabled: bool = False
    token_expires_at: datetime | None = None
    exchange_symbols: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _validate_common_config(self.account_id, self.api_key, self.broker_id, self.access_token, self.live_enabled, self.token_expires_at)
        _validate_exchange_symbols(self.exchange_symbols)

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "broker_id": self.broker_id,
                "live_enabled": self.live_enabled,
                "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
                "exchange_symbols": dict(self.exchange_symbols)}


class KiteAdapter(ConfiguredAdapter):
    provider_name = "zerodha_kite"

    def __init__(self, config: KiteConfig, *, transport: Any = None) -> None:
        expiry = _expiry_metadata(config.token_expires_at)
        super().__init__(account_id=config.account_id, broker_id=config.broker_id, live_enabled=config.live_enabled,
                         transport=transport, token_expiry=expiry, secrets=(config.api_key, config.access_token or ""),
                         capabilities=frozenset(AdapterCapability))
        self.config = config

    def map_symbol(self, symbol: str, *, exchange: str = "NSE") -> str:
        exchange, symbol = _validate_symbol_args(symbol, exchange)
        return self.config.exchange_symbols.get(f"{exchange}:{symbol}", symbol)

    def quote(self, symbol: str, *, exchange: str = "NSE") -> Quote:
        provider_symbol = self.map_symbol(symbol, exchange=exchange)
        response = self._call("quote", {"exchange": exchange.upper(), "symbol": provider_symbol})
        data = _mapping_data(response, self.provider_name)
        try:
            return Quote(symbol.upper(), exchange.upper(), Decimal(str(data["price"])), str(data.get("currency", "INR")),
                         _as_datetime(data.get("as_of") or response.get("as_of"), self.provider_name), provider_symbol)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise BrokerAdapterError("invalid_response", "quote response is malformed", provider=self.provider_name) from exc

    def account(self) -> AccountSnapshot:
        data = _mapping_data(self._call("account", {"account_id": self._account_id}), self.provider_name)
        return AccountSnapshot(self._account_id, str(data.get("currency", "INR")), _decimal_or_none(data.get("cash")),
                               _decimal_or_none(data.get("buying_power")), str(data["status"]) if data.get("status") is not None else None)

    def positions(self) -> tuple[Position, ...]:
        response = self._call("positions", {"account_id": self._account_id})
        rows = response.get("data", response)
        if not isinstance(rows, (list, tuple)):
            raise BrokerAdapterError("invalid_response", "positions response is malformed", provider=self.provider_name)
        try:
            return tuple(Position(str(row["symbol"]), Decimal(str(row.get("quantity", row.get("qty", 0)))),
                          _decimal_or_none(row.get("average_price")), str(row.get("currency", "INR")), row.get("exchange")) for row in rows)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise BrokerAdapterError("invalid_response", "positions response is malformed", provider=self.provider_name) from exc

    def submit_order(self, intent: OrderIntent) -> OrderReceipt:
        self._write_guard(intent)
        raise AssertionError("unreachable")

    def _submit_order(self, intent: OrderIntent, capability: _ExecutionCapability) -> OrderReceipt:
        intent = self._authorized_write(intent, capability)
        payload = self._payload(intent)
        response = self._call("submit_order", payload, write=True)
        return self._receipt(_mapping_data(response, self.provider_name))

    def _payload(self, intent: OrderIntent) -> dict[str, Any]:
        provider_symbol = self.map_symbol(intent.symbol)
        order_type = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.STOP: "SL-M", OrderType.STOP_LIMIT: "SL"}[intent.order_type]
        return {
            "account_id": self._account_id, "portfolio_id": intent.portfolio_id,
            "symbol": provider_symbol, "exchange": "NSE", "tradingsymbol": provider_symbol,
            "side": intent.side.value, "transaction_type": intent.side.value.upper(),
            "quantity": str(intent.quantity), "order_type": intent.order_type.value,
            "kite_order_type": order_type, "limit_price": _decimal_text(intent.limit_price),
            "price": _decimal_text(intent.limit_price), "stop_price": _decimal_text(intent.stop_price),
            "trigger_price": _decimal_text(intent.stop_price), "currency": intent.currency,
            "asset_class": intent.asset_class.value, "time_in_force": intent.time_in_force.value,
            "validity": intent.time_in_force.value.upper(), "idempotency_key": intent.idempotency_key,
            "client_order_id": intent.idempotency_key,
        }

    def cancel_order(self, broker_order_id: str) -> OrderReceipt:
        order_id = self._require_id(broker_order_id)
        raise self._direct_write_error()

    def _cancel_order(self, broker_order_id: str, capability: _ExecutionCapability) -> OrderReceipt:
        order_id = self._require_id(broker_order_id)
        if not isinstance(capability, _ExecutionCapability):
            raise self._direct_write_error()
        response = self._call("cancel_order", {"account_id": self._account_id, "order_id": order_id}, write=True)
        return self._receipt(_mapping_data(response, self.provider_name), default_status="canceled")

    def order_status(self, broker_order_id: str) -> OrderReceipt:
        order_id = self._require_id(broker_order_id)
        response = self._call("order_status", {"account_id": self._account_id, "order_id": order_id})
        return self._receipt(_mapping_data(response, self.provider_name), default_status="unknown")

    def reconcile(self) -> ReconciliationReport:
        data = _mapping_data(self._call("reconcile", {"account_id": self._account_id}), self.provider_name)
        orders_data = data.get("orders", [])
        if not isinstance(orders_data, (list, tuple)):
            raise BrokerAdapterError("invalid_response", "reconciliation response is malformed", provider=self.provider_name)
        orders = tuple(self._receipt(row, default_status="unknown") for row in orders_data)
        return ReconciliationReport(orders=orders, positions=self.positions(), warnings=tuple(self._redact(str(item)) for item in data.get("warnings", [])))

    def get_account(self) -> AccountSnapshot:
        return self.account()

    def get_positions(self) -> tuple[Position, ...]:
        return self.positions()

    def get_order_status(self, broker_order_id: str) -> OrderReceipt:
        return self.order_status(broker_order_id)

    def _direct_write_error(self) -> LiveExecutionDisabled:
        return LiveExecutionDisabled(self.provider_name, direct=self._live_enabled)


def _validate_common_config(account_id: str, credential: str, broker_id: str, access_token: str | None,
                            live_enabled: bool, token_expires_at: datetime | None) -> None:
    for name, value in (("account_id", account_id), ("credential", credential), ("broker_id", broker_id)):
        if not isinstance(value, str) or not value.strip():
            raise AdapterConfigurationError(f"{name} must be non-empty")
    if access_token is not None and (not isinstance(access_token, str) or not access_token.strip()):
        raise AdapterConfigurationError("access_token must be non-empty when provided")
    if not isinstance(live_enabled, bool):
        raise AdapterConfigurationError("live_enabled must be a boolean")
    if live_enabled and not access_token:
        raise AdapterConfigurationError("access_token is required when live_enabled is true")
    if token_expires_at is not None and (not isinstance(token_expires_at, datetime) or token_expires_at.tzinfo is None or token_expires_at.utcoffset() is None):
        raise AdapterConfigurationError("token_expires_at must be timezone-aware")


def _validate_exchange_symbols(value: Any) -> None:
    if not isinstance(value, dict):
        raise AdapterConfigurationError("exchange_symbols must be a mapping of EXCHANGE:SYMBOL to provider symbol")
    for key, mapped in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]+:[A-Za-z0-9._-]+", key):
            raise AdapterConfigurationError("exchange_symbols keys must use EXCHANGE:SYMBOL format")
        if not isinstance(mapped, str) or not mapped.strip():
            raise AdapterConfigurationError("exchange_symbols values must be non-empty strings")


def _expiry_metadata(value: datetime | None) -> TokenExpiryMetadata | None:
    return TokenExpiryMetadata(value, bool(value and value <= datetime.now(value.tzinfo))) if value else None


def _validate_symbol_args(symbol: str, exchange: str) -> tuple[str, str]:
    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(exchange, str) or not exchange.strip():
        raise ValueError("symbol and exchange must be non-empty strings")
    return exchange.upper(), symbol.upper()


def _mapping_data(response: Mapping[str, Any], provider: str) -> Mapping[str, Any]:
    data = response.get("data", response)
    if not isinstance(data, Mapping):
        raise BrokerAdapterError("invalid_response", "provider response data is malformed", provider=provider)
    return data


def _as_datetime(value: Any, provider: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BrokerAdapterError("invalid_response", "quote response is missing a valid timestamp", provider=provider) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise BrokerAdapterError("invalid_response", "quote timestamp must be timezone-aware", provider=provider)
    return result


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _decimal_text(value: Any) -> str | None:
    return None if value is None else format(value, "f")
