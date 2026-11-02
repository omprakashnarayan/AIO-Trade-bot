from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_dirs

_ORDER_STATUSES = frozenset({"pending", "submitted", "accepted", "partially_filled", "filled", "canceled", "rejected", "failed"})
_MAX_DECIMAL_DIGITS = 38
_MAX_DECIMAL_EXPONENT = 18
_MAX_DECIMAL_SERIALIZED_LENGTH = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    portfolio TEXT NOT NULL DEFAULT 'Main', symbol TEXT NOT NULL, label TEXT DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'INR', shares REAL NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY (portfolio, symbol)
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT NOT NULL DEFAULT 'Main', symbol TEXT NOT NULL,
    side TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'INR',
    fees REAL NOT NULL DEFAULT 0, trade_date TEXT NOT NULL, notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS price_cache (
    portfolio TEXT NOT NULL DEFAULT 'Main', symbol TEXT NOT NULL, price REAL NOT NULL,
    as_of TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (portfolio, symbol)
);
CREATE TABLE IF NOT EXISTS order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL, account_id TEXT, portfolio_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')), idempotency_key TEXT NOT NULL UNIQUE,
    order_payload TEXT NOT NULL CHECK (length(order_payload) > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','submitted','accepted','partially_filled','filled','canceled','rejected','failed')),
    external_order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK (account_id IS NOT NULL OR portfolio_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_order_intents_account_created ON order_intents(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_order_intents_status ON order_intents(status);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES order_intents(id),
    broker_id TEXT NOT NULL, account_id TEXT NOT NULL, external_fill_id TEXT NOT NULL,
    quantity TEXT NOT NULL CHECK (length(quantity) > 0), price TEXT NOT NULL CHECK (length(price) > 0),
    currency TEXT NOT NULL, execution_payload TEXT NOT NULL DEFAULT '{}', filled_at TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (broker_id, account_id, order_id, external_fill_id)
);
CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL, amount TEXT NOT NULL CHECK (length(amount) > 0),
    balance_after TEXT CHECK (balance_after IS NULL OR length(balance_after) > 0),
    entry_type TEXT NOT NULL, reference_id TEXT, created_at TEXT NOT NULL,
    CHECK (account_id <> '' OR portfolio_id <> '')
);
CREATE INDEX IF NOT EXISTS idx_cash_ledger_scope_created ON cash_ledger(account_id, portfolio_id, created_at);
CREATE TABLE IF NOT EXISTS cash_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL, balance TEXT NOT NULL CHECK (length(balance) > 0), updated_at TEXT NOT NULL,
    UNIQUE(account_id, portfolio_id, currency), CHECK (account_id <> '' OR portfolio_id <> '')
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    broker_id TEXT, account_id TEXT, portfolio_id TEXT, mode TEXT CHECK (mode IS NULL OR mode IN ('paper', 'live')),
    payload TEXT NOT NULL CHECK (length(payload) > 0), occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id, id);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""


def _canonical_decimal(value: Decimal | int | str | float, field_name: str, *, positive: bool = False, nonnegative: bool = False) -> str:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be decimal-compatible")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be decimal-compatible") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field_name} must be finite")
    sign, digits, exponent = decimal.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_DECIMAL_EXPONENT or len(format(decimal, "f")) > _MAX_DECIMAL_SERIALIZED_LENGTH:
        raise ValueError(f"{field_name} is outside the supported numeric range")
    if positive and decimal <= 0:
        raise ValueError(f"{field_name} must be positive")
    if nonnegative and decimal < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return format(decimal, "f")


def _canonical_json(value: Any, field_name: str) -> str:
    try:
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc


def _scope(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("scope identifiers must be strings")
    return value.strip()


def _currency(value: str) -> str:
    if not isinstance(value, str) or value.upper() not in {"USD", "INR"}:
        raise ValueError("currency must be the ISO code USD or INR")
    return value.upper()


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
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def insert_order_intent(self, intent: Any, *, broker_id: str = "paper", status: str = "pending", external_order_id: str | None = None) -> int:
        payload = intent.to_dict() if hasattr(intent, "to_dict") else intent
        if not isinstance(payload, dict):
            raise TypeError("intent must expose to_dict() or be a dictionary")
        if status not in _ORDER_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(_ORDER_STATUSES))}")
        encoded = _canonical_json(payload, "order payload")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute("""INSERT INTO order_intents(broker_id, account_id, portfolio_id, mode, idempotency_key, order_payload, status, external_order_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (broker_id, payload.get("account_id"), payload.get("portfolio_id"), payload["mode"], payload["idempotency_key"], encoded, status, external_order_id, now, now))
            return int(cursor.lastrowid)

    def get_order_intent(self, order_id: int | None = None, *, idempotency_key: str | None = None) -> sqlite3.Row | None:
        if order_id is None and idempotency_key is None:
            raise ValueError("order_id or idempotency_key is required")
        with self.connect() as conn:
            return conn.execute("SELECT * FROM order_intents WHERE id=?" if order_id is not None else "SELECT * FROM order_intents WHERE idempotency_key=?", (order_id if order_id is not None else idempotency_key,)).fetchone()

    def list_order_intents(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is not None and status not in _ORDER_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(_ORDER_STATUSES))}")
        with self.connect() as conn:
            return conn.execute("SELECT * FROM order_intents" + (" WHERE status=?" if status else "") + " ORDER BY id", (status,) if status else ()).fetchall()

    def insert_fill(self, fill: Any, *, order_id: int | None = None, broker_id: str | None = None, account_id: str | None = None, execution_payload: Any = None) -> int:
        payload = fill.to_dict() if hasattr(fill, "to_dict") else fill
        if not isinstance(payload, dict):
            raise TypeError("fill must expose to_dict() or be a dictionary")
        with self.connect() as conn:
            if order_id is None:
                row = conn.execute("SELECT id, broker_id, account_id FROM order_intents WHERE idempotency_key=?", (payload["intent_idempotency_key"],)).fetchone()
                if row is None:
                    raise ValueError("order for fill was not found")
                order_id = row["id"]
                broker_id = broker_id or row["broker_id"]
                account_id = account_id or row["account_id"] or ""
            else:
                row = conn.execute("SELECT broker_id, account_id FROM order_intents WHERE id=?", (order_id,)).fetchone()
                if row is None:
                    raise ValueError("order for fill was not found")
                broker_id = broker_id or row["broker_id"]
                account_id = account_id or row["account_id"] or ""
            quantity = _canonical_decimal(payload["quantity"], "fill quantity", positive=True)
            price = _canonical_decimal(payload["price"], "fill price", positive=True)
            encoded = _canonical_json({} if execution_payload is None else execution_payload, "execution payload")
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute("INSERT INTO fills(order_id, broker_id, account_id, external_fill_id, quantity, price, currency, execution_payload, filled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (order_id, broker_id, account_id, payload["fill_id"], quantity, price, _currency(payload["currency"]), encoded, payload["filled_at"], now))
            return int(cur.lastrowid)

    def get_fill(self, fill_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()

    def list_fills(self, order_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM fills" + (" WHERE order_id=?" if order_id else "") + " ORDER BY id", (order_id,) if order_id else ()).fetchall()

    def insert_cash_ledger(self, *, amount: Decimal | int | str | float, currency: str, entry_type: str, account_id: str | None = None, portfolio_id: str | None = None, balance_after: Decimal | int | str | float | None = None, reference_id: str | None = None, created_at: str | None = None) -> int:
        account_id, portfolio_id = _scope(account_id), _scope(portfolio_id)
        if not account_id and not portfolio_id:
            raise ValueError("account_id or portfolio_id is required")
        amount_text = _canonical_decimal(amount, "amount")
        balance_text = None if balance_after is None else _canonical_decimal(balance_after, "balance_after", nonnegative=True)
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO cash_ledger(account_id, portfolio_id, currency, amount, balance_after, entry_type, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (account_id, portfolio_id, _currency(currency), amount_text, balance_text, entry_type, reference_id, created_at or datetime.now(timezone.utc).isoformat()))
            return int(cur.lastrowid)

    def get_cash_ledger(self, entry_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_ledger WHERE id=?", (entry_id,)).fetchone()

    def list_cash_ledger(self, *, account_id: str | None = None, portfolio_id: str | None = None, currency: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for name, value in (("account_id", account_id), ("portfolio_id", portfolio_id), ("currency", currency)):
            if value is not None:
                clauses.append(name + "=?"); params.append(_scope(value).upper() if name == "currency" else _scope(value))
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_ledger" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id", params).fetchall()

    def upsert_cash_balance(self, *, balance: Decimal | int | str | float, currency: str, account_id: str | None = None, portfolio_id: str | None = None, updated_at: str | None = None) -> int:
        account_id, portfolio_id = _scope(account_id), _scope(portfolio_id)
        if not account_id and not portfolio_id:
            raise ValueError("account_id or portfolio_id is required")
        balance_text = _canonical_decimal(balance, "balance", nonnegative=True)
        now = updated_at or datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("INSERT INTO cash_balances(account_id, portfolio_id, currency, balance, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, portfolio_id, currency) DO UPDATE SET balance=excluded.balance, updated_at=excluded.updated_at", (account_id, portfolio_id, currency.upper(), balance_text, now))
            return int(conn.execute("SELECT id FROM cash_balances WHERE account_id=? AND portfolio_id=? AND currency=?", (account_id, portfolio_id, _currency(currency))).fetchone()[0])

    insert_cash_balance = upsert_cash_balance

    def get_cash_balance(self, *, currency: str, account_id: str | None = None, portfolio_id: str | None = None) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_balances WHERE account_id=? AND portfolio_id=? AND currency=?", (_scope(account_id), _scope(portfolio_id), _currency(currency))).fetchone()

    def list_cash_balances(self, *, account_id: str | None = None, portfolio_id: str | None = None, currency: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for name, value in (("account_id", account_id), ("portfolio_id", portfolio_id), ("currency", currency)):
            if value is not None:
                clauses.append(name + "=?"); params.append(_currency(value) if name == "currency" else _scope(value))
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_balances" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id", params).fetchall()

    def append_audit_event(self, *, event_type: str, entity_type: str, entity_id: str, payload: Any, broker_id: str | None = None, account_id: str | None = None, portfolio_id: str | None = None, mode: str | None = None, occurred_at: str | None = None) -> int:
        encoded = _canonical_json(payload, "audit payload")
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO audit_events(event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, encoded, occurred_at or created_at, created_at))
            return int(cur.lastrowid)

    def list_audit_events(self, *, entity_type: str | None = None, entity_id: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if entity_type is not None: clauses.append("entity_type=?"); params.append(entity_type)
        if entity_id is not None: clauses.append("entity_id=?"); params.append(entity_id)
        query = "SELECT * FROM audit_events" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
        if limit is not None: query += " LIMIT ?"; params.append(limit)
        with self.connect() as conn: return conn.execute(query, params).fetchall()

    def upsert_holding(self, symbol: str, label: str = "", shares: float = 0.0, avg_cost: float = 0.0, notes: str = "", portfolio: str = "Main", currency: str = "INR") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("INSERT INTO holdings(portfolio, symbol, label, currency, shares, avg_cost, notes, updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(portfolio, symbol) DO UPDATE SET label=excluded.label, currency=excluded.currency, shares=excluded.shares, avg_cost=excluded.avg_cost, notes=excluded.notes, updated_at=excluded.updated_at", (portfolio, symbol.upper(), label, currency.upper(), shares, avg_cost, notes, now))

    def adjust_holding(self, symbol: str, delta_shares: float, trade_price: float, side: str, fees: float = 0.0, trade_date: str | None = None, notes: str = "", portfolio: str = "Main", currency: str = "INR") -> None:
        delta = Decimal(_canonical_decimal(delta_shares, "delta_shares", positive=True))
        price = Decimal(_canonical_decimal(trade_price, "trade_price", positive=True))
        fee = Decimal(_canonical_decimal(fees, "fees", nonnegative=True))
        side = side.lower()
        if side not in {"buy", "sell"}: raise ValueError("side must be buy or sell")
        symbol, portfolio, currency = symbol.upper(), portfolio.strip() or "Main", currency.upper().strip() or "INR"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT portfolio, symbol, label, currency, shares, avg_cost, notes FROM holdings WHERE portfolio=? AND symbol=?", (portfolio, symbol)).fetchone()
            current_shares = Decimal(str(row["shares"])) if row else Decimal("0")
            current_avg = Decimal(str(row["avg_cost"])) if row else Decimal("0")
            if side == "buy":
                new_shares, new_avg = current_shares + delta, (current_shares * current_avg + delta * price + fee) / (current_shares + delta)
            else:
                new_shares = current_shares - delta
                if new_shares < 0: raise ValueError(f"Cannot sell more shares than owned for {symbol} in {portfolio}")
                new_avg = current_avg if new_shares else Decimal("0")
            conn.execute("INSERT INTO holdings(portfolio, symbol, label, currency, shares, avg_cost, notes, updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(portfolio, symbol) DO UPDATE SET label=excluded.label, currency=excluded.currency, shares=excluded.shares, avg_cost=excluded.avg_cost, notes=excluded.notes, updated_at=excluded.updated_at", (portfolio, symbol, row["label"] if row else "", currency, float(new_shares), float(new_avg), row["notes"] if row else notes, datetime.now(timezone.utc).isoformat()))
            conn.execute("INSERT INTO transactions(portfolio, symbol, side, shares, price, currency, fees, trade_date, notes) VALUES(?,?,?,?,?,?,?,?,?)", (portfolio, symbol, side, float(delta), float(price), currency, float(fee), trade_date or datetime.now(timezone.utc).date().isoformat(), notes))
            conn.commit()

    def list_holdings(self, portfolio: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn: return conn.execute("SELECT * FROM holdings" + (" WHERE portfolio=?" if portfolio else "") + " ORDER BY portfolio, symbol", (portfolio,) if portfolio else ()).fetchall()

    def list_portfolios(self) -> list[sqlite3.Row]:
        with self.connect() as conn: return conn.execute("SELECT portfolio, COALESCE(MAX(currency), 'INR') AS currency FROM holdings GROUP BY portfolio ORDER BY portfolio").fetchall()

    def list_transactions(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn: return conn.execute("SELECT * FROM transactions ORDER BY trade_date DESC, id DESC LIMIT ?", (limit,)).fetchall()

    def get_price_cache(self, portfolio: str, symbol: str) -> sqlite3.Row | None:
        with self.connect() as conn: return conn.execute("SELECT * FROM price_cache WHERE portfolio=? AND symbol=?", (portfolio, symbol.upper())).fetchone()

    def set_price_cache(self, portfolio: str, symbol: str, price: float, as_of: str, source: str) -> None:
        with self.connect() as conn: conn.execute("INSERT INTO price_cache(portfolio, symbol, price, as_of, source) VALUES(?,?,?,?,?) ON CONFLICT(portfolio, symbol) DO UPDATE SET price=excluded.price, as_of=excluded.as_of, source=excluded.source", (portfolio, symbol.upper(), price, as_of, source))

    def load_price_cache(self) -> dict[str, dict]:
        with self.connect() as conn: rows = conn.execute("SELECT * FROM price_cache").fetchall()
        return {f"{row['portfolio']}::{row['symbol']}": dict(row) for row in rows}
