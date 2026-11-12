"""Provider snapshot normalization and broker-scoped, read-only synchronization."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .brokers.base import BrokerAdapterError, Position, AccountSnapshot, OrderReceipt, normalize_status
from .db import Database


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value).strip()


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        value = default if value is None or value == "" else value
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("provider numeric field is malformed") from None
    if not result.is_finite():
        raise ValueError("provider numeric field is not finite")
    return result


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("provider timestamp is malformed") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("provider timestamp must be timezone-aware")
    return result.isoformat()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ProviderHolding:
    symbol: str
    quantity: Decimal
    average_price: Decimal | None = None
    currency: str = "INR"
    exchange: str = ""
    provider_id: str = ""


@dataclass(frozen=True, slots=True)
class ProviderCash:
    currency: str
    balance: Decimal
    buying_power: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ProviderOrder:
    provider_order_id: str
    symbol: str = ""
    status: str = "unknown"
    quantity: Decimal = Decimal("0")
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    submitted_at: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderFill:
    provider_fill_id: str
    provider_order_id: str
    symbol: str = ""
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    currency: str = "INR"
    filled_at: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    provider: str
    broker_id: str
    account_id: str
    source: str
    captured_at: str
    holdings: tuple[ProviderHolding, ...] = ()
    positions: tuple[ProviderHolding, ...] = ()
    cash: tuple[ProviderCash, ...] = ()
    orders: tuple[ProviderOrder, ...] = ()
    fills: tuple[ProviderFill, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedError:
    code: str
    message: str
    retryable: bool
    token_expired: bool = False


def _rows(value: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in keys:
            if isinstance(value.get(key), (list, tuple)):
                return [row for row in value[key] if isinstance(row, Mapping)]
        if isinstance(value.get("data"), (list, tuple)):
            return [row for row in value["data"] if isinstance(row, Mapping)]
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def normalize_provider_snapshot(provider: str, broker_id: str, account_id: str, response: Mapping[str, Any], *, source: str | None = None, captured_at: Any = None) -> ProviderSnapshot:
    if not isinstance(response, Mapping):
        raise ValueError("provider snapshot must be a mapping")
    root = response.get("data", response)
    if not isinstance(root, Mapping):
        raise ValueError("provider snapshot data is malformed")
    timestamp = _timestamp(captured_at or response.get("captured_at") or response.get("as_of") or now_utc())
    def holding(row: Mapping[str, Any]) -> ProviderHolding:
        symbol = _text(row.get("symbol") or row.get("tradingsymbol") or row.get("instrument_token"))
        if not symbol:
            raise ValueError("provider holding is missing symbol")
        return ProviderHolding(symbol.upper(), _decimal(row.get("quantity", row.get("qty", row.get("net_quantity", 0)))),
                               None if row.get("average_price", row.get("avg_price")) is None else _decimal(row.get("average_price", row.get("avg_price"))),
                               _text(row.get("currency"), "INR").upper(), _text(row.get("exchange")), _text(row.get("instrument_token") or row.get("id")))
    holdings = tuple(holding(row) for row in _rows(root, "holdings", "positions"))
    positions = tuple(holding(row) for row in _rows(root, "positions", "holdings"))
    cash_rows = _rows(root, "cash", "funds", "balances")
    if not cash_rows and any(k in root for k in ("cash", "balance", "available_cash")):
        cash_rows = [root]
    cash = tuple(ProviderCash(_text(row.get("currency"), "INR").upper(), _decimal(row.get("balance", row.get("cash", row.get("available_cash", 0)))), None if row.get("buying_power") is None else _decimal(row["buying_power"])) for row in cash_rows)
    orders = []
    for row in _rows(root, "orders"):
        oid = _text(row.get("order_id") or row.get("orderId") or row.get("id"))
        if not oid: raise ValueError("provider order is missing order ID")
        orders.append(ProviderOrder(oid, _text(row.get("symbol") or row.get("tradingsymbol")), normalize_status(row.get("status")).value, _decimal(row.get("quantity")), _decimal(row.get("filled_quantity", row.get("filled_qty", 0))), None if row.get("average_price") is None else _decimal(row["average_price"]), _timestamp(row["submitted_at"]) if row.get("submitted_at") else None, dict(row)))
    fills = []
    for row in _rows(root, "fills", "executions"):
        fid = _text(row.get("fill_id") or row.get("trade_id") or row.get("id")); oid = _text(row.get("order_id") or row.get("orderId"))
        if not fid or not oid: raise ValueError("provider fill is missing IDs")
        fills.append(ProviderFill(fid, oid, _text(row.get("symbol") or row.get("tradingsymbol")), _decimal(row.get("quantity", row.get("qty"))), _decimal(row.get("price")), _text(row.get("currency"), "INR").upper(), _timestamp(row["filled_at"]) if row.get("filled_at") else None, dict(row)))
    return ProviderSnapshot(provider, broker_id, account_id, source or provider, timestamp, holdings, positions, cash, tuple(orders), tuple(fills))


class SnapshotAdapter(Protocol):
    metadata: Any
    def snapshot(self) -> Mapping[str, Any]: ...


class ProviderSyncService:
    def __init__(self, db: Database):
        self.db = db

    def sync(self, adapter: SnapshotAdapter) -> dict[str, Any]:
        meta = adapter.metadata
        started = now_utc()
        run_id = self.db.create_provider_sync_run(meta.broker_id, meta.account_id, meta.provider_name, started_at=started)
        try:
            response = adapter.snapshot()
            snapshot = normalize_provider_snapshot(meta.provider_name, meta.broker_id, meta.account_id, response, source=meta.provider_name)
            counts = self.db.upsert_provider_snapshot(snapshot)
            reconciliation = self.db.reconcile_provider_snapshot(snapshot)
            finished = now_utc()
            self.db.finish_provider_sync_run(run_id, status="succeeded", finished_at=finished, error=None, counts=counts, reconciliation_id=reconciliation["id"])
            return {"run_id": run_id, "status": "succeeded", "source": snapshot.source, "captured_at": snapshot.captured_at, "counts": counts, "reconciliation": reconciliation}
        except BrokerAdapterError as exc:
            finished = now_utc(); error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "token_expired": exc.code == "token_expired"}
            self.db.finish_provider_sync_run(run_id, status="stale" if error["token_expired"] else "error", finished_at=finished, error=error, counts={})
            return {"run_id": run_id, "status": "stale" if error["token_expired"] else "error", "error": error}
        except (ValueError, TypeError) as exc:
            error = {"code": "normalization_error", "message": str(exc), "retryable": False, "token_expired": False}
            self.db.finish_provider_sync_run(run_id, status="error", finished_at=now_utc(), error=error, counts={})
            return {"run_id": run_id, "status": "error", "error": error}
