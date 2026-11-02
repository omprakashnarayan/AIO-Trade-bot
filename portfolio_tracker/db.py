from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    portfolio TEXT NOT NULL DEFAULT 'Main',
    symbol TEXT NOT NULL,
    label TEXT DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'INR',
    shares REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portfolio, symbol)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio TEXT NOT NULL DEFAULT 'Main',
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    fees REAL NOT NULL DEFAULT 0,
    trade_date TEXT NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_cache (
    portfolio TEXT NOT NULL DEFAULT 'Main',
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (portfolio, symbol)
);

CREATE TABLE IF NOT EXISTS order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id TEXT NOT NULL,
    account_id TEXT,
    portfolio_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    idempotency_key TEXT NOT NULL UNIQUE,
    order_payload TEXT NOT NULL CHECK (length(order_payload) > 0),
    status TEXT NOT NULL DEFAULT 'pending',
    external_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (account_id IS NOT NULL OR portfolio_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_order_intents_account_created
    ON order_intents(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_order_intents_status
    ON order_intents(status);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES order_intents(id),
    external_fill_id TEXT,
    quantity TEXT NOT NULL CHECK (length(quantity) > 0),
    price TEXT NOT NULL CHECK (length(price) > 0),
    currency TEXT NOT NULL,
    execution_payload TEXT NOT NULL DEFAULT '{}',
    filled_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    portfolio_id TEXT,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL CHECK (length(amount) > 0),
    balance_after TEXT CHECK (balance_after IS NULL OR length(balance_after) > 0),
    entry_type TEXT NOT NULL,
    reference_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (account_id IS NOT NULL OR portfolio_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_cash_ledger_scope_created
    ON cash_ledger(account_id, portfolio_id, created_at);

CREATE TABLE IF NOT EXISTS cash_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    portfolio_id TEXT,
    currency TEXT NOT NULL,
    balance TEXT NOT NULL CHECK (length(balance) > 0),
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, portfolio_id, currency),
    CHECK (account_id IS NOT NULL OR portfolio_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    broker_id TEXT,
    account_id TEXT,
    portfolio_id TEXT,
    mode TEXT CHECK (mode IS NULL OR mode IN ('paper', 'live')),
    payload TEXT NOT NULL CHECK (length(payload) > 0),
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_entity
    ON audit_events(entity_type, entity_id, id);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
"""


def _canonical_decimal(value: Decimal | int | str, field_name: str) -> str:
    """Validate a monetary/quantity value and return its canonical text form."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be decimal-compatible")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be decimal-compatible") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return format(decimal, "f")


@dataclass
class Holding:
    portfolio: str
    symbol: str
    label: str
    currency: str
    shares: float
    avg_cost: float
    notes: str = ""


class Database:
    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self.path = Path(path or DB_PATH)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def insert_order_intent(
        self,
        intent: Any,
        *,
        broker_id: str = "paper",
        status: str = "pending",
        external_order_id: str | None = None,
    ) -> int:
        """Insert one order intent and reject duplicate idempotency keys.

        ``intent`` is normally an ``OrderIntent``; any object exposing
        ``to_dict()`` is accepted so persistence stays broker-neutral.
        """
        payload = intent.to_dict() if hasattr(intent, "to_dict") else intent
        if not isinstance(payload, dict):
            raise TypeError("intent must expose to_dict() or be a dictionary")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_intents(
                    broker_id, account_id, portfolio_id, mode, idempotency_key,
                    order_payload, status, external_order_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    broker_id,
                    payload.get("account_id"),
                    payload.get("portfolio_id"),
                    payload["mode"],
                    payload["idempotency_key"],
                    encoded,
                    status,
                    external_order_id,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_order_intent(
        self,
        order_id: int | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> sqlite3.Row | None:
        """Read an order intent by internal ID or idempotency key."""
        if order_id is None and idempotency_key is None:
            raise ValueError("order_id or idempotency_key is required")
        with self.connect() as conn:
            if order_id is not None:
                return conn.execute("SELECT * FROM order_intents WHERE id=?", (order_id,)).fetchone()
            return conn.execute(
                "SELECT * FROM order_intents WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()

    def list_order_intents(self, status: str | None = None) -> list[sqlite3.Row]:
        """Read order intents in insertion order, optionally filtered by status."""
        with self.connect() as conn:
            if status is None:
                return conn.execute("SELECT * FROM order_intents ORDER BY id").fetchall()
            return conn.execute(
                "SELECT * FROM order_intents WHERE status=? ORDER BY id", (status,)
            ).fetchall()

    def append_audit_event(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | str,
        broker_id: str | None = None,
        account_id: str | None = None,
        portfolio_id: str | None = None,
        mode: str | None = None,
        occurred_at: str | None = None,
    ) -> int:
        """Append one immutable audit event and return its internal ID."""
        encoded = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_events(
                    event_type, entity_type, entity_id, broker_id, account_id,
                    portfolio_id, mode, payload, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    entity_type,
                    entity_id,
                    broker_id,
                    account_id,
                    portfolio_id,
                    mode,
                    encoded,
                    occurred_at or created_at,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Read append-only audit events in chronological insertion order."""
        query = "SELECT * FROM audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def upsert_holding(
        self,
        symbol: str,
        label: str = "",
        shares: float = 0.0,
        avg_cost: float = 0.0,
        notes: str = "",
        portfolio: str = "Main",
        currency: str = "INR",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO holdings(portfolio, symbol, label, currency, shares, avg_cost, notes, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(portfolio, symbol) DO UPDATE SET
                    label=excluded.label,
                    currency=excluded.currency,
                    shares=excluded.shares,
                    avg_cost=excluded.avg_cost,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (portfolio, symbol.upper(), label, currency.upper(), shares, avg_cost, notes, now),
            )
            conn.commit()

    def adjust_holding(
        self,
        symbol: str,
        delta_shares: float,
        trade_price: float,
        side: str,
        fees: float = 0.0,
        trade_date: str | None = None,
        notes: str = "",
        portfolio: str = "Main",
        currency: str = "INR",
    ) -> None:
        symbol = symbol.upper()
        portfolio = portfolio.strip() or "Main"
        currency = currency.upper().strip() or "INR"
        trade_date = trade_date or datetime.now(timezone.utc).date().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                "SELECT portfolio, symbol, label, currency, shares, avg_cost, notes FROM holdings WHERE portfolio=? AND symbol=?",
                (portfolio, symbol),
            )
            row = cur.fetchone()
            current_shares = float(row["shares"]) if row else 0.0
            current_avg = float(row["avg_cost"]) if row else 0.0

            if side.lower() == "buy":
                new_shares = current_shares + delta_shares
                total_cost = current_shares * current_avg + delta_shares * trade_price + fees
                new_avg = total_cost / new_shares if new_shares else 0.0
            elif side.lower() == "sell":
                new_shares = current_shares - delta_shares
                if new_shares < -1e-9:
                    raise ValueError(f"Cannot sell more shares than owned for {symbol} in {portfolio}")
                new_avg = current_avg if new_shares > 0 else 0.0
            else:
                raise ValueError("side must be buy or sell")

            conn.execute(
                """
                INSERT INTO holdings(portfolio, symbol, label, currency, shares, avg_cost, notes, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(portfolio, symbol) DO UPDATE SET
                    label=excluded.label,
                    currency=excluded.currency,
                    shares=excluded.shares,
                    avg_cost=excluded.avg_cost,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    portfolio,
                    symbol,
                    row["label"] if row else "",
                    currency,
                    new_shares,
                    new_avg,
                    row["notes"] if row else notes,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO transactions(portfolio, symbol, side, shares, price, currency, fees, trade_date, notes)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (portfolio, symbol, side.lower(), delta_shares, trade_price, currency, fees, trade_date, notes),
            )
            conn.commit()

    def list_holdings(self, portfolio: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if portfolio is None:
                return conn.execute("SELECT * FROM holdings ORDER BY portfolio, symbol").fetchall()
            return conn.execute(
                "SELECT * FROM holdings WHERE portfolio=? ORDER BY symbol",
                (portfolio,),
            ).fetchall()

    def list_portfolios(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT portfolio, COALESCE(MAX(currency), 'INR') AS currency FROM holdings GROUP BY portfolio ORDER BY portfolio"
            ).fetchall()

    def list_transactions(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM transactions ORDER BY trade_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def get_price_cache(self, portfolio: str, symbol: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM price_cache WHERE portfolio=? AND symbol=?",
                (portfolio, symbol.upper()),
            ).fetchone()

    def set_price_cache(self, portfolio: str, symbol: str, price: float, as_of: str, source: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO price_cache(portfolio, symbol, price, as_of, source)
                VALUES(?,?,?,?,?)
                ON CONFLICT(portfolio, symbol) DO UPDATE SET
                    price=excluded.price,
                    as_of=excluded.as_of,
                    source=excluded.source
                """,
                (portfolio, symbol.upper(), price, as_of, source),
            )
            conn.commit()

    def load_price_cache(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM price_cache").fetchall()
        return {f"{row['portfolio']}::{row['symbol']}": dict(row) for row in rows}
