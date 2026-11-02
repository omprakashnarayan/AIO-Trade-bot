"""Deterministic, local-only paper execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from .db import Database
from .trading_models import AssetClass, ExecutionMode, Fill, OrderIntent, OrderSide, OrderType


class PaperBrokerError(Exception):
    """Base class for paper execution errors."""


class LiveModeError(PaperBrokerError):
    """Raised when a live intent is sent to the paper broker."""


class InsufficientCashError(PaperBrokerError):
    """Reserved for callers that want exception-based cash handling."""


class InsufficientPositionError(PaperBrokerError):
    """Reserved for callers that want exception-based position handling."""


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    order_id: int
    status: str
    fill: Fill | None
    cash_balance: Decimal
    total: Decimal
    error: str | None = None
    reused: bool = False

    @property
    def filled(self) -> bool:
        return self.status == "filled"


class PaperBroker:
    """Execute market equity intents against SQLite state, without a network call."""

    def __init__(self, db: Database, *, broker_id: str = "paper", fee_rate: Decimal | int | str = Decimal("0"), slippage_bps: Decimal | int | str = Decimal("0"), clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.broker_id = broker_id
        self.fee_rate = self._decimal(fee_rate, "fee_rate")
        self.slippage_bps = self._decimal(slippage_bps, "slippage_bps")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must be nonnegative")
        if self.slippage_bps < 0 or self.slippage_bps >= Decimal("10000"):
            raise ValueError("slippage_bps must be in the range [0, 10000)")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.init_db()

    def seed_cash(self, amount: Decimal | int | str, *, currency: str, account_id: str | None = None, portfolio_id: str | None = None) -> int:
        return self.db.upsert_cash_balance(balance=amount, currency=currency, account_id=account_id, portfolio_id=portfolio_id, broker_id=self.broker_id)

    def submit(self, intent: OrderIntent, price: Decimal | int | str) -> PaperExecutionResult:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        if intent.mode is ExecutionMode.LIVE:
            raise LiveModeError("PaperBroker only executes PAPER mode intents")
        if intent.mode is not ExecutionMode.PAPER:
            raise ValueError("unsupported execution mode")
        if intent.asset_class is not AssetClass.EQUITY or intent.side not in {OrderSide.BUY, OrderSide.SELL}:
            raise ValueError("PaperBroker supports long-only equity BUY and SELL")
        if intent.order_type is not OrderType.MARKET:
            raise ValueError("PaperBroker supports market orders only")
        market_price = self._decimal(price, "price")
        if market_price <= 0:
            raise ValueError("price must be positive")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        scope_account = intent.account_id or ""
        scope_portfolio = intent.portfolio_id if intent.portfolio_id is not None else ""
        cash_row = self.db.get_cash_balance(currency=intent.currency, account_id=scope_account, portfolio_id=scope_portfolio, broker_id=self.broker_id)
        cash = Decimal(cash_row["balance"]) if cash_row else Decimal("0")
        holding = self.db.get_paper_holding(broker_id=self.broker_id, portfolio=scope_portfolio, symbol=intent.symbol)
        owned = Decimal(holding["shares"]) if holding else Decimal("0")
        quantity = Decimal(intent.quantity)
        multiplier = Decimal("1") + (self.slippage_bps / Decimal("10000")) * (Decimal("1") if intent.side is OrderSide.BUY else Decimal("-1"))
        fill_price = market_price * multiplier
        notional = quantity * fill_price
        fee = notional * self.fee_rate
        fee = fee.quantize(Decimal("0.000000000000000001"))
        total = notional + fee if intent.side is OrderSide.BUY else notional - fee
        total = total.quantize(Decimal("0.000000000000000001"))
        reason = None
        if intent.side is OrderSide.BUY and cash < total:
            reason = f"insufficient cash: required {total}, available {cash}"
        elif intent.side is OrderSide.SELL and owned < quantity:
            reason = f"insufficient position: required {quantity}, owned {owned}"
        fill_id = "fill-" + hashlib.sha256((self.broker_id + "\x1f" + intent.idempotency_key).encode("utf-8")).hexdigest()
        outcome = self.db.execute_paper_submission(intent=intent, broker_id=self.broker_id, quantity=quantity, fill_price=fill_price, fee=fee, total=total, now=now.isoformat(), fill_id=fill_id, reason=reason, audit_callback=self._audit)
        fill = self._fill_from_row(outcome["fill"], intent.idempotency_key) if outcome["fill"] else None
        execution_total = total
        if fill and outcome["reused"]:
            execution_total = Decimal(json.loads(outcome["fill"]["execution_payload"]).get("total", "0"))
        return PaperExecutionResult(outcome["order_id"], outcome["status"], fill, outcome["cash"], Decimal("0") if outcome["status"] == "rejected" else execution_total, reason if outcome["status"] == "rejected" else None, outcome["reused"])

    execute = submit
    submit_order = submit

    def _audit(self, conn, event_type: str, entity_type: str, entity_id: str, intent: OrderIntent, details: dict, occurred_at: str) -> None:
        return None

    @staticmethod
    def _fill_from_row(row, key: str) -> Fill:
        return Fill(row["external_fill_id"], key, Decimal(row["quantity"]), Decimal(row["price"]), row["currency"], datetime.fromisoformat(row["filled_at"]))

    @staticmethod
    def _decimal(value: Decimal | int | str, name: str) -> Decimal:
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be decimal-compatible") from exc
        if not result.is_finite():
            raise ValueError(f"{name} must be finite")
        return result
