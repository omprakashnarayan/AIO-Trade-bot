"""Provider-specific Upstox adapter boundary with no built-in network client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import AdapterConfigurationError
from .kite import KiteAdapter, KiteConfig, _validate_common_config, _validate_exchange_symbols
from ..trading_models import OrderIntent, OrderType


@dataclass(frozen=True, slots=True)
class UpstoxConfig:
    account_id: str
    client_id: str = field(repr=False)
    access_token: str | None = field(default=None, repr=False)
    broker_id: str = "upstox"
    live_enabled: bool = False
    token_expires_at: datetime | None = None
    exchange_symbols: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _validate_common_config(self.account_id, self.client_id, self.broker_id, self.access_token, self.live_enabled, self.token_expires_at)
        _validate_exchange_symbols(self.exchange_symbols)

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "broker_id": self.broker_id,
                "live_enabled": self.live_enabled,
                "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
                "exchange_symbols": dict(self.exchange_symbols)}


class UpstoxAdapter(KiteAdapter):
    provider_name = "upstox"

    def __init__(self, config: UpstoxConfig, *, transport: Any = None) -> None:
        self.upstox_config = config
        # Keep Upstox credentials and mappings separate from Kite's provider model.
        super().__init__(KiteConfig(
            account_id=config.account_id, api_key=config.client_id, access_token=config.access_token,
            broker_id=config.broker_id, live_enabled=config.live_enabled,
            token_expires_at=config.token_expires_at, exchange_symbols=config.exchange_symbols), transport=transport)
        self.config = config

    def _payload(self, intent: OrderIntent) -> dict[str, Any]:
        provider_symbol = self.map_symbol(intent.symbol)
        order_type = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.STOP: "SL", OrderType.STOP_LIMIT: "SL"}[intent.order_type]
        return {
            "account_id": self._account_id, "portfolio_id": intent.portfolio_id,
            "instrument_token": provider_symbol, "symbol": provider_symbol,
            "transaction_type": intent.side.value.upper(), "side": intent.side.value,
            "quantity": str(intent.quantity), "order_type": order_type,
            "limit_price": _decimal_text(intent.limit_price), "price": _decimal_text(intent.limit_price),
            "stop_price": _decimal_text(intent.stop_price), "trigger_price": _decimal_text(intent.stop_price),
            "currency": intent.currency, "asset_class": intent.asset_class.value,
            "time_in_force": intent.time_in_force.value, "validity": intent.time_in_force.value.upper(),
            "idempotency_key": intent.idempotency_key, "client_order_id": intent.idempotency_key,
        }

def _decimal_text(value: Any) -> str | None:
    return None if value is None else format(value, "f")
