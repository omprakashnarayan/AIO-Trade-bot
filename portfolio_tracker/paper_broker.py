"""Deterministic, local-only paper execution."""

from __future__ import annotations

import json
import sqlite3
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

    def __init__(
        self,
        db: Database,
        *,
        broker_id: str = "paper",
        fee_rate: Decimal | int | str = Decimal("0"),
        slippage_bps: Decimal | int | str = Decimal("0"),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.broker_id = broker_id
        self.fee_rate = self._decimal(fee_rate, "fee_rate")
        self.slippage_bps = self._decimal(slippage_bps, "slippage_bps")
        if self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("fee_rate and slippage_bps must be nonnegative")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.init_db()

    def seed_cash(self, amount: Decimal | int | str, *, currency: str, account_id: str | None = None, portfolio_id: str | None = None) -> int:
        """Set the starting cash balance for one account/portfolio scope."""
        return self.db.upsert_cash_balance(balance=amount, currency=currency, account_id=account_id, portfolio_id=portfolio_id)

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

        existing = self.db.get_order_intent(idempotency_key=intent.idempotency_key)
        if existing is not None:
            if json.loads(existing["order_payload"]) != intent.to_dict():
                raise ValueError("idempotency key is already bound to a different intent")
            return self._result_from_existing(existing)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        scope_portfolio = intent.portfolio_id or intent.account_id
        scope_account = intent.account_id or ""
        quantity = Decimal(intent.quantity)
        multiplier = Decimal("1") + (self.slippage_bps / Decimal("10000")) * (1 if intent.side is OrderSide.BUY else -1)
        fill_price = market_price * multiplier
        notional = quantity * fill_price
        fee = notional * self.fee_rate
        total = notional + fee if intent.side is OrderSide.BUY else notional - fee
        cash_scope = (scope_account, scope_portfolio, intent.currency)

        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM order_intents WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
            if row is not None:
                return self._result_from_existing(row)
            payload = json.dumps(intent.to_dict(), sort_keys=True, separators=(",", ":"))
            iso_now = now.isoformat()
            cur = conn.execute(
                "INSERT INTO order_intents(broker_id, account_id, portfolio_id, mode, idempotency_key, order_payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, intent.idempotency_key, payload, "accepted", iso_now, iso_now),
            )
            order_id = int(cur.lastrowid)

            cash_row = conn.execute("SELECT balance FROM cash_balances WHERE account_id=? AND portfolio_id=? AND currency=?", cash_scope).fetchone()
            cash = Decimal(cash_row["balance"]) if cash_row else Decimal("0")
            holding = conn.execute("SELECT shares, avg_cost FROM holdings WHERE portfolio=? AND symbol=?", (scope_portfolio, intent.symbol)).fetchone()
            owned = Decimal(str(holding["shares"])) if holding else Decimal("0")
            reason = None
            if intent.side is OrderSide.BUY and cash < total:
                reason = f"insufficient cash: required {total}, available {cash}"
            elif intent.side is OrderSide.SELL and owned < quantity:
                reason = f"insufficient position: required {quantity}, owned {owned}"
            if reason:
                conn.execute("UPDATE order_intents SET status='rejected', updated_at=? WHERE id=?", (iso_now, order_id))
                self._audit(conn, "order.rejected", "order_intent", str(order_id), intent, {"reason": reason}, iso_now)
                return PaperExecutionResult(order_id, "rejected", None, cash, Decimal("0"), reason)

            new_cash = cash - total if intent.side is OrderSide.BUY else cash + total
            conn.execute("INSERT INTO cash_balances(account_id, portfolio_id, currency, balance, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, portfolio_id, currency) DO UPDATE SET balance=excluded.balance, updated_at=excluded.updated_at", (*cash_scope[:2], cash_scope[2], format(new_cash, "f"), iso_now))
            amount = -total if intent.side is OrderSide.BUY else total
            conn.execute("INSERT INTO cash_ledger(account_id, portfolio_id, currency, amount, balance_after, entry_type, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*cash_scope[:2], cash_scope[2], format(amount, "f"), format(new_cash, "f"), "trade", str(order_id), iso_now))

            new_shares = owned + quantity if intent.side is OrderSide.BUY else owned - quantity
            old_avg = Decimal(str(holding["avg_cost"])) if holding else Decimal("0")
            new_avg = ((owned * old_avg) + (quantity * fill_price) + fee) / new_shares if intent.side is OrderSide.BUY else (old_avg if new_shares else Decimal("0"))
            conn.execute("INSERT INTO holdings(portfolio, symbol, label, currency, shares, avg_cost, notes, updated_at) VALUES (?, ?, '', ?, ?, ?, '', ?) ON CONFLICT(portfolio, symbol) DO UPDATE SET shares=excluded.shares, avg_cost=excluded.avg_cost, currency=excluded.currency, updated_at=excluded.updated_at", (scope_portfolio, intent.symbol, intent.currency, float(new_shares), float(new_avg), iso_now))
            conn.execute("INSERT INTO transactions(portfolio, symbol, side, shares, price, currency, fees, trade_date, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (scope_portfolio, intent.symbol, intent.side.value, float(quantity), float(fill_price), intent.currency, float(fee), iso_now[:10], "paper broker"))

            fill = Fill(f"fill-{intent.idempotency_key}", intent.idempotency_key, quantity, fill_price, intent.currency, now)
            conn.execute("INSERT INTO fills(order_id, broker_id, account_id, external_fill_id, quantity, price, currency, execution_payload, filled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (order_id, self.broker_id, intent.account_id or "", fill.fill_id, format(quantity, "f"), format(fill_price, "f"), intent.currency, json.dumps({"fee": format(fee, "f"), "slippage_bps": format(self.slippage_bps, "f"), "total": format(total, "f")}, sort_keys=True, separators=(",", ":")), now.isoformat(), iso_now))
            conn.execute("UPDATE order_intents SET status='filled', updated_at=? WHERE id=?", (iso_now, order_id))
            self._audit(conn, "order.filled", "order_intent", str(order_id), intent, {"fill_id": fill.fill_id, "total": format(total, "f")}, iso_now)
            return PaperExecutionResult(order_id, "filled", fill, new_cash, total)

    execute = submit
    submit_order = submit

    @staticmethod
    def _decimal(value: Decimal | int | str, name: str) -> Decimal:
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be decimal-compatible") from exc
        if not result.is_finite():
            raise ValueError(f"{name} must be finite")
        return result

    def _audit(self, conn: sqlite3.Connection, event_type: str, entity_type: str, entity_id: str, intent: OrderIntent, details: dict, occurred_at: str) -> None:
        payload = {"intent": intent.to_dict(), **details}
        conn.execute("INSERT INTO audit_events(event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_type, entity_type, entity_id, self.broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, json.dumps(payload, sort_keys=True, separators=(",", ":")), occurred_at, occurred_at))

    def _result_from_existing(self, row: sqlite3.Row) -> PaperExecutionResult:
        fill_row = self.db.list_fills(row["id"])
        fill = None
        if fill_row:
            f = fill_row[0]
            fill = Fill(f["external_fill_id"], json.loads(row["order_payload"])["idempotency_key"], f["quantity"], f["price"], f["currency"], datetime.fromisoformat(f["filled_at"]))
        payload = json.loads(row["order_payload"])
        balance = self.db.get_cash_balance(currency=payload["currency"], account_id=payload.get("account_id"), portfolio_id=payload.get("portfolio_id"))
        cash = Decimal(balance["balance"]) if balance else Decimal("0")
        total = Decimal("0")
        if fill:
            fill_db = fill_row[0]
            execution = json.loads(fill_db["execution_payload"])
            total = Decimal(execution.get("total", format(fill.quantity * fill.price, "f")))
        return PaperExecutionResult(row["id"], row["status"], fill, cash, total, reused=True)

