"""Broker-neutral adapter contracts with fail-closed execution semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..trading_models import OrderIntent


class BrokerAdapterError(Exception):
    """Normalized provider error; never includes credentials or raw auth headers."""

    def __init__(self, code: str, message: str, *, provider: str, retryable: bool = False,
                 status: str | None = None, rate_limit: "RateLimitMetadata | None" = None,
                 token_expiry: "TokenExpiryMetadata | None" = None) -> None:
        self.code = code
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.status = status
        self.rate_limit = rate_limit
        self.token_expiry = token_expiry
        super().__init__(f"{provider}: {code}: {message}")


class AdapterConfigurationError(ValueError):
    """Configuration is incomplete or unsafe for the requested operation."""


class LiveExecutionDisabled(BrokerAdapterError):
    """Live execution is disabled unless explicitly enabled in provider config."""

    def __init__(self, provider: str) -> None:
        super().__init__("live_disabled", "LIVE execution is disabled by configuration", provider=provider)


class AdapterCapability(str, Enum):
    QUOTES = "quotes"
    ACCOUNT = "account"
    POSITIONS = "positions"
    SUBMIT = "submit"
    CANCEL = "cancel"
    STATUS = "status"
    RECONCILE = "reconcile"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RateLimitMetadata:
    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TokenExpiryMetadata:
    expires_at: datetime | None = None
    expired: bool = False


@dataclass(frozen=True, slots=True)
class BrokerMetadata:
    provider_name: str
    account_id: str
    broker_id: str
    mode: str
    supported_capabilities: frozenset[AdapterCapability]
    rate_limit: RateLimitMetadata | None = None
    token_expiry: TokenExpiryMetadata | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "account_id": self.account_id,
            "broker_id": self.broker_id,
            "mode": self.mode,
            "supported_capabilities": sorted(capability.value for capability in self.supported_capabilities),
            "rate_limit": _to_public_dict(self.rate_limit),
            "token_expiry": _to_public_dict(self.token_expiry),
        }


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    exchange: str
    price: Decimal
    currency: str
    as_of: datetime
    provider_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    average_price: Decimal | None = None
    currency: str = "INR"
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: str
    currency: str
    cash: Decimal | None = None
    buying_power: Decimal | None = None
    raw_status: str | None = None


@dataclass(frozen=True, slots=True)
class OrderReceipt:
    broker_order_id: str
    status: OrderStatus
    provider_status: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    orders: tuple[OrderReceipt, ...] = ()
    positions: tuple[Position, ...] = ()
    warnings: tuple[str, ...] = ()


def _to_public_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "__dataclass_fields__"):
        return {name: _to_public_dict(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def normalize_status(value: Any) -> OrderStatus:
    """Map common provider statuses to the internal status vocabulary."""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "open": OrderStatus.SUBMITTED, "trigger_pending": OrderStatus.PENDING,
        "complete": OrderStatus.FILLED, "completed": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELED, "cancel": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED, "error": OrderStatus.FAILED,
    }
    return mapping.get(text, OrderStatus(text) if text in {item.value for item in OrderStatus} else OrderStatus.UNKNOWN)


@runtime_checkable
class BrokerAdapter(Protocol):
    metadata: BrokerMetadata

    def quote(self, symbol: str, *, exchange: str = "NSE") -> Quote: ...
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> tuple[Position, ...]: ...
    def submit_order(self, intent: OrderIntent) -> OrderReceipt: ...
    def cancel_order(self, broker_order_id: str) -> OrderReceipt: ...
    def order_status(self, broker_order_id: str) -> OrderReceipt: ...
    def reconcile(self) -> ReconciliationReport: ...


class Transport(Protocol):
    def __call__(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


def require_intent(intent: OrderIntent) -> OrderIntent:
    if not isinstance(intent, OrderIntent):
        raise TypeError("broker adapters accept only a validated OrderIntent")
    return intent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConfiguredAdapter:
    """Shared mechanics for official API adapters.

    The transport is deliberately injectable. No HTTP client is created here,
    and paper intents are left to ExecutionGateway/PaperBroker so risk cannot
    be bypassed by calling an adapter directly.
    """

    provider_name: str

    def __init__(self, *, account_id: str, broker_id: str, live_enabled: bool = False,
                 transport: Transport | None = None, token_expiry: TokenExpiryMetadata | None = None,
                 capabilities: frozenset[AdapterCapability]) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise AdapterConfigurationError("account_id must be non-empty")
        if not isinstance(broker_id, str) or not broker_id.strip():
            raise AdapterConfigurationError("broker_id must be non-empty")
        if not isinstance(live_enabled, bool):
            raise AdapterConfigurationError("live_enabled must be a boolean")
        self._account_id = account_id
        self._broker_id = broker_id
        self._live_enabled = live_enabled
        self._transport = transport
        self._token_expiry = token_expiry
        mode = "live" if live_enabled else "paper/read_only"
        self.metadata = BrokerMetadata(self.provider_name, account_id, broker_id, mode, capabilities, token_expiry=token_expiry)

    @property
    def live_enabled(self) -> bool:
        return self._live_enabled

    def _call(self, operation: str, payload: Mapping[str, Any], *, write: bool = False) -> Mapping[str, Any]:
        if write and not self._live_enabled:
            raise LiveExecutionDisabled(self.provider_name)
        if self._transport is None:
            raise BrokerAdapterError("transport_unconfigured", "no injectable transport is configured", provider=self.provider_name)
        try:
            result = self._transport(operation, dict(payload))
        except BrokerAdapterError:
            raise
        except Exception as exc:
            raise BrokerAdapterError("transport_error", "provider transport failed", provider=self.provider_name, retryable=True) from exc
        if not isinstance(result, Mapping):
            raise BrokerAdapterError("invalid_response", "provider response must be a mapping", provider=self.provider_name)
        error = result.get("error")
        if error:
            raise self._normalize_error(error, result)
        return result

    def _normalize_error(self, error: Any, response: Mapping[str, Any] | None = None) -> BrokerAdapterError:
        response = response or {}
        if isinstance(error, Mapping):
            code = str(error.get("code") or error.get("error_type") or "provider_error")
            message = str(error.get("message") or error.get("description") or "provider request failed")
            provider_status = error.get("status")
        else:
            code, message, provider_status = "provider_error", str(error), None
        return BrokerAdapterError(code, message, provider=self.provider_name,
                                  retryable=code in {"rate_limit", "timeout", "temporarily_unavailable"},
                                  status=str(provider_status) if provider_status is not None else None,
                                  rate_limit=self.metadata.rate_limit, token_expiry=self.metadata.token_expiry)

    def _write_guard(self, intent: OrderIntent) -> OrderIntent:
        require_intent(intent)
        if intent.mode.value == "paper":
            raise BrokerAdapterError("paper_requires_gateway", "route PAPER intents through ExecutionGateway/PaperBroker", provider=self.provider_name)
        if not self._live_enabled:
            raise LiveExecutionDisabled(self.provider_name)
        return intent

    @staticmethod
    def _require_id(value: str, field_name: str = "broker_order_id") -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be non-empty")
        return value
