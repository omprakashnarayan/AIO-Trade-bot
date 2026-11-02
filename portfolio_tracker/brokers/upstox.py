"""Upstox API adapter boundary with no built-in network client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import AdapterConfigurationError
from .kite import KiteAdapter, KiteConfig


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
        for name in ("account_id", "client_id", "broker_id"):
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


class UpstoxAdapter(KiteAdapter):
    provider_name = "upstox"

    def __init__(self, config: UpstoxConfig, *, transport=None) -> None:
        self.upstox_config = config
        # Reuse the transport-only operation boundary. Client ID is never sent
        # in payloads and is intentionally not serialized by this adapter.
        super().__init__(KiteConfig(account_id=config.account_id, api_key=config.client_id,
                                    access_token=config.access_token, broker_id=config.broker_id,
                                    live_enabled=config.live_enabled, token_expires_at=config.token_expires_at,
                                    exchange_symbols=config.exchange_symbols), transport=transport)
        self.config = config
