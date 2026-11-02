"""Kite Connect adapter boundary; transport is injectable and live writes fail closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .base import (
    AdapterCapability, AdapterConfigurationError, BrokerAdapterError, BrokerMetadata,
    ConfiguredAdapter, OrderReceipt, OrderStatus, Position, Quote, AccountSnapshot,
    ReconciliationReport, TokenExpiryMetadata, normalize_status, require_intent,
)


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
        for name in ("account_id", "api_key", "broker_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AdapterConfigurationError(f"{name} must be non-empty")
        if self.access_token is not None and (not isinstance(self.access_token, str) or not self.access_token.strip()):
            raise AdapterConfigurationError("access_token must be non-empty when provided")
        if not isinstance(self.live_enabled, bool):
            raise AdapterConfigurationError("live_enabled must be a boolean")
        if self.live_enabled and not self.access_token:
            raise AdapterConfigurationError("access_token is required when live_enabled is true")

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "broker_id": self.broker_id,
                "live_enabled": self.live_enabled, "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
                "exchange_symbols": dict(self.exchange_symbols)}


class KiteAdapter(ConfiguredAdapter):
    provider_name = "zerodha_kite"

    def __init__(self, config: KiteConfig, *, transport=None) -> None:
        expiry = TokenExpiryMetadata(config.token_expires_at, bool(config.token_expires_at and config.token_expires_at <= datetime.now(config.token_expires_at.tzinfo))) if config.token_expires_at else None
        super().__init__(account_id=config.account_id, broker_id=config.broker_id, live_enabled=config.live_enabled,
                         transport=transport, token_expiry=expiry,
                         capabilities=frozenset(AdapterCapability))
        self.config = config

    def map_symbol(self, symbol: str, *, exchange: str = "NSE") -> str:
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(exchange, str) or not exchange.strip():
            raise ValueError("symbol and exchange must be non-empty strings")
        key = f"{exchange.upper()}:{symbol.upper()}"
        return self.config.exchange_symbols.get(key, symbol.upper())

    def quote(self, symbol: str, *, exchange: str = "NSE") -> Quote:
        provider_symbol = self.map_symbol(symbol, exchange=exchange)
        response = self._call("quote", {"exchange": exchange.upper(), "symbol": provider_symbol})
        data = response.get("data", response)
        return Quote(symbol.upper(), exchange.upper(), Decimal(str(data["price"])), str(data.get("currency", "INR")), _as_datetime(data.get("as_of") or response.get("as_of")), provider_symbol)

    def account(self) -> AccountSnapshot:
        data = self._call("account", {"account_id": self._account_id}).get("data", {})
        return AccountSnapshot(self._account_id, str(data.get("currency", "INR")), _decimal_or_none(data.get("cash")), _decimal_or_none(data.get("buying_power")), data.get("status"))

    def positions(self) -> tuple[Position, ...]:
        rows = self._call("positions", {"account_id": self._account_id}).get("data", [])
        return tuple(Position(str(row["symbol"]), Decimal(str(row.get("quantity", row.get("qty", 0)))), _decimal_or_none(row.get("average_price")), str(row.get("currency", "INR")), row.get("exchange")) for row in rows)

    def submit_order(self, intent) -> OrderReceipt:
        intent = self._write_guard(intent)
        payload = {"account_id": self._account_id, "symbol": self.map_symbol(intent.symbol), "side": intent.side.value, "order_type": intent.order_type.value, "quantity": str(intent.quantity), "time_in_force": intent.time_in_force.value, "idempotency_key": intent.idempotency_key}
        response = self._call("submit_order", payload, write=True)
        data = response.get("data", response)
        return OrderReceipt(str(data.get("order_id") or data.get("orderId")), normalize_status(data.get("status", "submitted")), data.get("status"), data.get("message"))

    def cancel_order(self, broker_order_id: str) -> OrderReceipt:
        order_id = self._require_id(broker_order_id)
        data = self._call("cancel_order", {"account_id": self._account_id, "order_id": order_id}, write=True).get("data", {})
        return OrderReceipt(order_id, normalize_status(data.get("status", "canceled")), data.get("status"), data.get("message"))

    def order_status(self, broker_order_id: str) -> OrderReceipt:
        order_id = self._require_id(broker_order_id)
        data = self._call("order_status", {"account_id": self._account_id, "order_id": order_id}).get("data", {})
        return OrderReceipt(order_id, normalize_status(data.get("status")), data.get("status"), data.get("message"))

    def reconcile(self) -> ReconciliationReport:
        data = self._call("reconcile", {"account_id": self._account_id}).get("data", {})
        orders = tuple(OrderReceipt(str(row["order_id"]), normalize_status(row.get("status")), row.get("status"), row.get("message")) for row in data.get("orders", []))
        return ReconciliationReport(orders=orders, positions=self.positions(), warnings=tuple(str(item) for item in data.get("warnings", [])))

    def get_account(self) -> AccountSnapshot:
        return self.account()

    def get_positions(self) -> tuple[Position, ...]:
        return self.positions()

    def get_order_status(self, broker_order_id: str) -> OrderReceipt:
        return self.order_status(broker_order_id)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise BrokerAdapterError("invalid_response", "quote response is missing a valid timestamp", provider="zerodha_kite")


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))
