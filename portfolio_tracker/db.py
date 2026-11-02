from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_dirs
from .trading_models import OrderIntent

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
CREATE TABLE IF NOT EXISTS indmoney_holdings (
    portfolio TEXT NOT NULL, symbol TEXT NOT NULL, label TEXT DEFAULT '', currency TEXT NOT NULL,
    shares TEXT NOT NULL CHECK (length(shares) > 0), avg_cost TEXT NOT NULL CHECK (length(avg_cost) > 0),
    account TEXT DEFAULT '', source TEXT NOT NULL, source_imported_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL, PRIMARY KEY (portfolio, symbol)
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
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL, amount TEXT NOT NULL CHECK (length(amount) > 0),
    balance_after TEXT CHECK (balance_after IS NULL OR length(balance_after) > 0),
    entry_type TEXT NOT NULL, reference_id TEXT, created_at TEXT NOT NULL,
    CHECK (account_id <> '' OR portfolio_id <> '')
);
CREATE TABLE IF NOT EXISTS cash_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL, balance TEXT NOT NULL CHECK (length(balance) > 0), updated_at TEXT NOT NULL,
    UNIQUE(broker_id, account_id, portfolio_id, currency), CHECK (account_id <> '' OR portfolio_id <> '')
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    broker_id TEXT, account_id TEXT, portfolio_id TEXT, mode TEXT CHECK (mode IS NULL OR mode IN ('paper', 'live')),
    payload TEXT NOT NULL CHECK (length(payload) > 0), occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id, id);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TABLE IF NOT EXISTS paper_holdings (
    broker_id TEXT NOT NULL DEFAULT 'paper', account_id TEXT NOT NULL DEFAULT '', portfolio TEXT NOT NULL, symbol TEXT NOT NULL, currency TEXT NOT NULL,
    shares TEXT NOT NULL CHECK (length(shares) > 0), avg_cost TEXT NOT NULL CHECK (length(avg_cost) > 0),
    updated_at TEXT NOT NULL, PRIMARY KEY (broker_id, account_id, portfolio, symbol)
);
CREATE TABLE IF NOT EXISTS paper_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '', portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, shares TEXT NOT NULL CHECK (length(shares) > 0), price TEXT NOT NULL CHECK (length(price) > 0),
    currency TEXT NOT NULL, fees TEXT NOT NULL CHECK (length(fees) > 0), trade_date TEXT NOT NULL, notes TEXT DEFAULT ''
);
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


_MIGRATION_TABLES = {
    "order_intents": """
        CREATE TABLE order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL, account_id TEXT, portfolio_id TEXT,
            mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')), idempotency_key TEXT NOT NULL UNIQUE,
            order_payload TEXT NOT NULL CHECK (length(order_payload) > 0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','submitted','accepted','partially_filled','filled','canceled','rejected','failed')),
            external_order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK (account_id IS NOT NULL OR portfolio_id IS NOT NULL)
        )
    """,
    "fills": """
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES order_intents(id),
            broker_id TEXT NOT NULL, account_id TEXT NOT NULL, external_fill_id TEXT NOT NULL,
            quantity TEXT NOT NULL CHECK (length(quantity) > 0), price TEXT NOT NULL CHECK (length(price) > 0),
            currency TEXT NOT NULL, execution_payload TEXT NOT NULL DEFAULT '{}', filled_at TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (broker_id, account_id, order_id, external_fill_id)
        )
    """,
    "cash_ledger": """
        CREATE TABLE cash_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL, amount TEXT NOT NULL CHECK (length(amount) > 0),
            balance_after TEXT CHECK (balance_after IS NULL OR length(balance_after) > 0),
            entry_type TEXT NOT NULL, reference_id TEXT, created_at TEXT NOT NULL,
            CHECK (account_id <> '' OR portfolio_id <> '')
        )
    """,
    "cash_balances": """
        CREATE TABLE cash_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '', portfolio_id TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL, balance TEXT NOT NULL CHECK (length(balance) > 0), updated_at TEXT NOT NULL,
            UNIQUE(broker_id, account_id, portfolio_id, currency), CHECK (account_id <> '' OR portfolio_id <> '')
        )
    """,
    "audit_events": """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            broker_id TEXT, account_id TEXT, portfolio_id TEXT, mode TEXT CHECK (mode IS NULL OR mode IN ('paper', 'live')),
            payload TEXT NOT NULL CHECK (length(payload) > 0), occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
        )
    """,
}

_EXPECTED_COLUMNS = {
    "order_intents": {
        "id": (0, None), "broker_id": (1, None), "account_id": (0, None), "portfolio_id": (0, None),
        "mode": (1, None), "idempotency_key": (1, None), "order_payload": (1, None), "status": (1, "'pending'"),
        "external_order_id": (0, None), "created_at": (1, None), "updated_at": (1, None),
    },
    "fills": {
        "id": (0, None), "order_id": (1, None), "broker_id": (1, None), "account_id": (1, None),
        "external_fill_id": (1, None), "quantity": (1, None), "price": (1, None), "currency": (1, None),
        "execution_payload": (1, "'{}'"), "filled_at": (1, None), "created_at": (1, None),
    },
    "cash_ledger": {
        "id": (0, None), "broker_id": (1, "''"), "account_id": (1, "''"), "portfolio_id": (1, "''"), "currency": (1, None),
        "amount": (1, None), "balance_after": (0, None), "entry_type": (1, None), "reference_id": (0, None), "created_at": (1, None),
    },
    "cash_balances": {
        "id": (0, None), "broker_id": (1, "''"), "account_id": (1, "''"), "portfolio_id": (1, "''"), "currency": (1, None),
        "balance": (1, None), "updated_at": (1, None),
    },
    "audit_events": {
        "id": (0, None), "event_type": (1, None), "entity_type": (1, None), "entity_id": (1, None),
        "broker_id": (0, None), "account_id": (0, None), "portfolio_id": (0, None), "mode": (0, None),
        "payload": (1, None), "occurred_at": (1, None), "created_at": (1, None),
    },
}

_EXPECTED_INDEXES = {
    "order_intents": {"idx_order_intents_account_created": (False, ("account_id", "created_at")), "idx_order_intents_status": (False, ("status",))},
    "fills": {"idx_fills_order_id": (False, ("order_id",))},
    "cash_ledger": {"idx_cash_ledger_scope_created": (False, ("broker_id", "account_id", "portfolio_id", "created_at"))},
    "cash_balances": {},
    "audit_events": {"idx_audit_events_entity": (False, ("entity_type", "entity_id", "id"))},
}

_EXPECTED_CHECKS = {
    "order_intents": ("mode in ('paper', 'live')", "length(order_payload) > 0", "account_id is not null or portfolio_id is not null"),
    "fills": ("length(quantity) > 0", "length(price) > 0"),
    "cash_ledger": ("length(amount) > 0", "balance_after is null or length(balance_after) > 0", "account_id <> '' or portfolio_id <> ''"),
    "cash_balances": ("length(balance) > 0", "account_id <> '' or portfolio_id <> ''"),
    "audit_events": ("mode is null or mode in ('paper', 'live')", "length(payload) > 0"),
}

_STATUS_CHECK_PATTERN = re.compile(r"check\s*\(\s*status\s+in\s*\(([^()]*)\)\s*\)", re.IGNORECASE)


def _status_check_values(sql: str) -> frozenset[str] | None:
    matches = _STATUS_CHECK_PATTERN.findall(sql)
    if len(matches) != 1:
        return None
    values = []
    for item in matches[0].split(","):
        match = re.fullmatch(r"\s*'((?:''|[^'])*)'\s*", item)
        if match is None:
            return None
        values.append(match.group(1).replace("''", "'").lower())
    if len(values) != len(set(values)):
        return None
    return frozenset(values)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return (row["sql"] or "").lower() if row else ""


def _table_indexes(conn: sqlite3.Connection, table: str) -> dict[str, tuple[bool, tuple[str, ...]]]:
    indexes = {}
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = row["name"]
        columns = tuple(info["name"] for info in conn.execute(f"PRAGMA index_info({name})") if info["name"] is not None)
        indexes[name] = (bool(row["unique"]), columns)
    return indexes


def _needs_migration(conn: sqlite3.Connection, table: str) -> bool:
    info = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, (notnull, default) in _EXPECTED_COLUMNS[table].items():
        column = info.get(name)
        if column is None or bool(column["notnull"]) != bool(notnull):
            return True
        if default is not None and (column["dflt_value"] or "").replace(" ", "") != default.replace(" ", ""):
            return True
    if set(info) != set(_EXPECTED_COLUMNS[table]):
        return True
    sql = _table_sql(conn, table)
    if "autoincrement" not in sql:
        return True
    if any(check not in sql for check in _EXPECTED_CHECKS[table]):
        return True
    if table == "order_intents" and _status_check_values(sql) != _ORDER_STATUSES:
        return True
    indexes = _table_indexes(conn, table)
    for name, expected in _EXPECTED_INDEXES[table].items():
        if indexes.get(name) != expected:
            return True
    if table in {"order_intents", "fills", "cash_balances"}:
        unique_columns = {
            "order_intents": ("idempotency_key",),
            "fills": ("broker_id", "account_id", "order_id", "external_fill_id"),
            "cash_balances": ("broker_id", "account_id", "portfolio_id", "currency"),
        }[table]
        if not any(unique and columns == unique_columns for unique, columns in indexes.values()):
            return True
    if table == "fills":
        foreign_keys = {(row["table"], row["from"], row["to"]) for row in conn.execute("PRAGMA foreign_key_list(fills)")}
        if ("order_intents", "order_id", "id") not in foreign_keys:
            return True
    return False


def _migrate_legacy_tables(conn: sqlite3.Connection) -> None:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rebuild = {table for table in _MIGRATION_TABLES if table in tables and _needs_migration(conn, table)}
    if not rebuild:
        return

    saved = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in rebuild}
    order_scope = {
        row["id"]: (row.get("broker_id") or "paper", row.get("account_id") or "")
        for row in saved.get("order_intents", [])
    }
    if "order_intents" not in saved:
        order_scope.update({
            row["id"]: (row["broker_id"] or "paper", row["account_id"] or "")
            for row in conn.execute("SELECT id, broker_id, account_id FROM order_intents")
        })
    for table in rebuild:
        conn.execute(f"DROP TABLE {table}")
    for table in rebuild:
        conn.execute(_MIGRATION_TABLES[table])

    if "order_intents" in rebuild:
        for row in saved["order_intents"]:
            account_id, portfolio_id = row.get("account_id"), row.get("portfolio_id")
            if account_id is None and portfolio_id is None:
                account_id = "legacy"
            status = row.get("status") if row.get("status") in _ORDER_STATUSES else "pending"
            mode = row.get("mode") if row.get("mode") in {"paper", "live"} else "paper"
            conn.execute(
                "INSERT INTO order_intents(id, broker_id, account_id, portfolio_id, mode, idempotency_key, order_payload, status, external_order_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row.get("broker_id") or "paper", account_id, portfolio_id, mode, row.get("idempotency_key") or f"legacy-{row['id']}", row.get("order_payload") or "{}", status, row.get("external_order_id"), row.get("created_at") or datetime.now(timezone.utc).isoformat(), row.get("updated_at") or row.get("created_at") or datetime.now(timezone.utc).isoformat()),
            )
    if "fills" in rebuild:
        for row in saved["fills"]:
            broker_id, account_id = order_scope.get(row["order_id"], (row.get("broker_id") or "paper", row.get("account_id") or ""))
            external_fill_id = row.get("external_fill_id") or f"legacy-fill-{row['id']}"
            conn.execute(
                "INSERT INTO fills(id, order_id, broker_id, account_id, external_fill_id, quantity, price, currency, execution_payload, filled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["order_id"], broker_id, account_id, external_fill_id, row.get("quantity") or "0", row.get("price") or "0", row.get("currency") or "INR", row.get("execution_payload") or "{}", row.get("filled_at") or datetime.now(timezone.utc).isoformat(), row.get("created_at") or datetime.now(timezone.utc).isoformat()),
            )
    for table in ("cash_ledger", "cash_balances"):
        if table not in rebuild:
            continue
        for row in saved[table]:
            account_id, portfolio_id = row.get("account_id") or "", row.get("portfolio_id") or ""
            if not account_id and not portfolio_id:
                account_id = "legacy"
            if table == "cash_ledger":
                conn.execute("INSERT INTO cash_ledger(id, broker_id, account_id, portfolio_id, currency, amount, balance_after, entry_type, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (row["id"], row.get("broker_id") or "", account_id, portfolio_id, row.get("currency") or "INR", row.get("amount") or "0", row.get("balance_after"), row.get("entry_type") or "legacy", row.get("reference_id"), row.get("created_at") or datetime.now(timezone.utc).isoformat()))
            else:
                conn.execute("INSERT INTO cash_balances(id, broker_id, account_id, portfolio_id, currency, balance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (row["id"], row.get("broker_id") or "", account_id, portfolio_id, row.get("currency") or "INR", row.get("balance") or "0", row.get("updated_at") or datetime.now(timezone.utc).isoformat()))
    if "audit_events" in rebuild:
        for row in saved["audit_events"]:
            conn.execute("INSERT INTO audit_events(id, event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (row["id"], row.get("event_type") or "legacy", row.get("entity_type") or "legacy", row.get("entity_id") or str(row["id"]), row.get("broker_id"), row.get("account_id"), row.get("portfolio_id"), row.get("mode") if row.get("mode") in {None, "paper", "live"} else None, row.get("payload") or "{}", row.get("occurred_at") or datetime.now(timezone.utc).isoformat(), row.get("created_at") or datetime.now(timezone.utc).isoformat()))


def _ensure_supporting_objects(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_order_intents_account_created ON order_intents(account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_order_intents_status ON order_intents(status);
        CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
        CREATE INDEX IF NOT EXISTS idx_cash_ledger_scope_created ON cash_ledger(broker_id, account_id, portfolio_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id, id);
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        """
    )


def _migrate_paper_holdings(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "paper_holdings")
    if columns and "account_id" in columns:
        pk = {row["name"] for row in conn.execute("PRAGMA table_info(paper_holdings)") if row["pk"]}
        if pk == {"broker_id", "account_id", "portfolio", "symbol"}:
            return
    if not columns:
        return
    conn.execute("ALTER TABLE paper_holdings RENAME TO paper_holdings_legacy")
    conn.execute("""CREATE TABLE paper_holdings (
        broker_id TEXT NOT NULL DEFAULT 'paper', account_id TEXT NOT NULL DEFAULT '', portfolio TEXT NOT NULL, symbol TEXT NOT NULL, currency TEXT NOT NULL,
        shares TEXT NOT NULL CHECK (length(shares) > 0), avg_cost TEXT NOT NULL CHECK (length(avg_cost) > 0),
        updated_at TEXT NOT NULL, PRIMARY KEY (broker_id, account_id, portfolio, symbol)
    )""")
    broker = "broker_id" if "broker_id" in columns else "'paper'"
    account = "account_id" if "account_id" in columns else "''"
    conn.execute(f"INSERT INTO paper_holdings(broker_id, account_id, portfolio, symbol, currency, shares, avg_cost, updated_at) SELECT {broker}, {account}, portfolio, symbol, currency, shares, avg_cost, updated_at FROM paper_holdings_legacy")
    conn.execute("DROP TABLE paper_holdings_legacy")


def _migrate_paper_transactions(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "paper_transactions")
    if not columns or "account_id" in columns:
        return
    conn.execute("ALTER TABLE paper_transactions RENAME TO paper_transactions_legacy")
    conn.execute("""CREATE TABLE paper_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '', portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, shares TEXT NOT NULL CHECK (length(shares) > 0), price TEXT NOT NULL CHECK (length(price) > 0),
        currency TEXT NOT NULL, fees TEXT NOT NULL CHECK (length(fees) > 0), trade_date TEXT NOT NULL, notes TEXT DEFAULT ''
    )""")
    conn.execute("INSERT INTO paper_transactions(id, broker_id, account_id, portfolio, symbol, side, shares, price, currency, fees, trade_date, notes) SELECT id, broker_id, '', portfolio, symbol, side, shares, price, currency, fees, trade_date, notes FROM paper_transactions_legacy")
    conn.execute("DROP TABLE paper_transactions_legacy")


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
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(SCHEMA)
            _migrate_legacy_tables(conn)
            _migrate_paper_holdings(conn)
            _migrate_paper_transactions(conn)
            _ensure_supporting_objects(conn)
            conn.execute("PRAGMA foreign_keys = ON")

    def insert_order_intent(self, intent: Any, *, broker_id: str = "paper", status: str = "pending", external_order_id: str | None = None) -> int:
        if isinstance(intent, OrderIntent):
            validated = intent
        elif isinstance(intent, dict):
            try:
                validated = OrderIntent(**intent)
            except (TypeError, ValueError) as exc:
                raise ValueError("intent dictionary is not a valid OrderIntent") from exc
        else:
            raise TypeError("intent must be an OrderIntent or a dictionary")
        payload = validated.to_dict()
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

    @staticmethod
    def paper_order_key(broker_id: str, account_id: str | None, portfolio_id: str | None, idempotency_key: str) -> str:
        namespace = "\x1f".join((broker_id, account_id or "", portfolio_id or "", idempotency_key))
        return "paper-" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()

    def get_scoped_order_intent(self, *, broker_id: str, account_id: str | None, portfolio_id: str | None, idempotency_key: str) -> sqlite3.Row | None:
        storage_key = self.paper_order_key(broker_id, account_id, portfolio_id, idempotency_key)
        with self.connect() as conn:
            return conn.execute("SELECT * FROM order_intents WHERE broker_id=? AND account_id IS ? AND portfolio_id IS ? AND idempotency_key=?", (broker_id, account_id, portfolio_id, storage_key)).fetchone()

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
            else:
                row = conn.execute("SELECT broker_id, account_id, idempotency_key FROM order_intents WHERE id=?", (order_id,)).fetchone()
                if row is None:
                    raise ValueError("order for fill was not found")
                if payload["intent_idempotency_key"] != row["idempotency_key"]:
                    raise ValueError("fill does not belong to the referenced order")
            expected_broker_id = row["broker_id"]
            expected_account_id = row["account_id"] or ""
            if broker_id is not None and broker_id != expected_broker_id:
                raise ValueError("broker_id does not match the referenced order")
            if account_id is not None and account_id != expected_account_id:
                raise ValueError("account_id does not match the referenced order")
            broker_id, account_id = expected_broker_id, expected_account_id
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

    def insert_cash_ledger(self, *, amount: Decimal | int | str | float, currency: str, entry_type: str, account_id: str | None = None, portfolio_id: str | None = None, broker_id: str = "", balance_after: Decimal | int | str | float | None = None, reference_id: str | None = None, created_at: str | None = None) -> int:
        account_id, portfolio_id = _scope(account_id), _scope(portfolio_id)
        if not account_id and not portfolio_id:
            raise ValueError("account_id or portfolio_id is required")
        amount_text = _canonical_decimal(amount, "amount")
        balance_text = None if balance_after is None else _canonical_decimal(balance_after, "balance_after", nonnegative=True)
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO cash_ledger(broker_id, account_id, portfolio_id, currency, amount, balance_after, entry_type, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker_id, account_id, portfolio_id, _currency(currency), amount_text, balance_text, entry_type, reference_id, created_at or datetime.now(timezone.utc).isoformat()))
            return int(cur.lastrowid)

    def get_cash_ledger(self, entry_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_ledger WHERE id=?", (entry_id,)).fetchone()

    def list_cash_ledger(self, *, account_id: str | None = None, portfolio_id: str | None = None, currency: str | None = None, broker_id: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for name, value in (("broker_id", broker_id), ("account_id", account_id), ("portfolio_id", portfolio_id), ("currency", currency)):
            if value is not None:
                clauses.append(name + "=?"); params.append(_scope(value).upper() if name == "currency" else _scope(value))
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cash_ledger" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id", params).fetchall()

    def upsert_cash_balance(self, *, balance: Decimal | int | str | float, currency: str, account_id: str | None = None, portfolio_id: str | None = None, broker_id: str = "", updated_at: str | None = None) -> int:
        account_id, portfolio_id = _scope(account_id), _scope(portfolio_id)
        if not account_id and not portfolio_id:
            raise ValueError("account_id or portfolio_id is required")
        balance_text = _canonical_decimal(balance, "balance", nonnegative=True)
        now = updated_at or datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("INSERT INTO cash_balances(broker_id, account_id, portfolio_id, currency, balance, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(broker_id, account_id, portfolio_id, currency) DO UPDATE SET balance=excluded.balance, updated_at=excluded.updated_at", (broker_id, account_id, portfolio_id, _currency(currency), balance_text, now))
            return int(conn.execute("SELECT id FROM cash_balances WHERE broker_id=? AND account_id=? AND portfolio_id=? AND currency=?", (broker_id, account_id, portfolio_id, _currency(currency))).fetchone()[0])

    insert_cash_balance = upsert_cash_balance

    def get_cash_balance(self, *, currency: str, account_id: str | None = None, portfolio_id: str | None = None, broker_id: str | None = None) -> sqlite3.Row | None:
        with self.connect() as conn:
            if broker_id is not None:
                return conn.execute("SELECT * FROM cash_balances WHERE broker_id=? AND account_id=? AND portfolio_id=? AND currency=?", (broker_id, _scope(account_id), _scope(portfolio_id), _currency(currency))).fetchone()
            row = conn.execute("SELECT * FROM cash_balances WHERE broker_id='paper' AND account_id=? AND portfolio_id=? AND currency=?", (_scope(account_id), _scope(portfolio_id), _currency(currency))).fetchone()
            return row or conn.execute("SELECT * FROM cash_balances WHERE broker_id='' AND account_id=? AND portfolio_id=? AND currency=?", (_scope(account_id), _scope(portfolio_id), _currency(currency))).fetchone()

    def list_cash_balances(self, *, account_id: str | None = None, portfolio_id: str | None = None, currency: str | None = None, broker_id: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for name, value in (("broker_id", broker_id), ("account_id", account_id), ("portfolio_id", portfolio_id), ("currency", currency)):
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

    @staticmethod
    def upsert_indmoney_holding_conn(conn: sqlite3.Connection, *, portfolio: str, symbol: str, label: str, account: str, shares: Decimal, avg_cost: Decimal, source: str, source_imported_at: str, ingested_at: str) -> None:
        shares_text = _canonical_decimal(shares, "shares", nonnegative=True)
        avg_cost_text = _canonical_decimal(avg_cost, "avg_cost", nonnegative=True)
        conn.execute("""INSERT INTO indmoney_holdings
            (portfolio, symbol, label, currency, shares, avg_cost, account, source, source_imported_at, ingested_at)
            VALUES (?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(portfolio, symbol) DO UPDATE SET label=excluded.label,
            shares=excluded.shares, avg_cost=excluded.avg_cost, account=excluded.account,
            source=excluded.source, source_imported_at=excluded.source_imported_at,
            ingested_at=excluded.ingested_at""", (portfolio, symbol, label, shares_text, avg_cost_text, account, source, source_imported_at, ingested_at))

    def upsert_indmoney_holding(self, **kwargs: Any) -> None:
        with self.connect() as conn:
            self.upsert_indmoney_holding_conn(conn, **kwargs)

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

    def get_paper_holding(self, *, broker_id: str = "paper", account_id: str | None = None, portfolio: str, symbol: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            if account_id is None:
                return conn.execute("SELECT * FROM paper_holdings WHERE broker_id=? AND portfolio=? AND symbol=? ORDER BY account_id", (broker_id, portfolio, symbol)).fetchone()
            return conn.execute("SELECT * FROM paper_holdings WHERE broker_id=? AND account_id=? AND portfolio=? AND symbol=?", (broker_id, account_id, portfolio, symbol)).fetchone()

    def list_paper_transactions(self, *, broker_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM paper_transactions" + (" WHERE broker_id=?" if broker_id is not None else "") + " ORDER BY id", (broker_id,) if broker_id is not None else ()).fetchall()

    def execute_paper_submission(self, *, intent: OrderIntent, broker_id: str, quantity: Decimal, fill_price: Decimal, fee: Decimal, total: Decimal, now: str, fill_id: str, reason: str | None = None, audit_callback=None, max_position_weight: Decimal | None = None, portfolio_value: Decimal | None = None) -> dict[str, Any]:
        account_id = intent.account_id or ""
        portfolio_id = intent.portfolio_id if intent.portfolio_id is not None else ""
        storage_key = self.paper_order_key(broker_id, intent.account_id, intent.portfolio_id, intent.idempotency_key)
        payload = _canonical_json(intent.to_dict(), "order payload")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM order_intents WHERE broker_id=? AND account_id IS ? AND portfolio_id IS ? AND idempotency_key=?", (broker_id, intent.account_id, intent.portfolio_id, storage_key)).fetchone()
            if row is not None:
                if row["order_payload"] != payload:
                    raise ValueError("conflicting order payload for reused idempotency key")
                if audit_callback:
                    audit_callback(conn, "order.retry", "order_intent", str(row["id"]), intent, {"reused": True}, now)
                self._append_audit_event_conn(conn, "order.retry", "order_intent", str(row["id"]), {"intent": intent.to_dict(), "reused": True}, broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, now)
                return {"order_id": row["id"], "status": row["status"], "reused": True, "cash": self._cash_in_conn(conn, intent.currency, account_id, portfolio_id, broker_id), "fill": conn.execute("SELECT * FROM fills WHERE order_id=? ORDER BY id LIMIT 1", (row["id"],)).fetchone()}
            cur = conn.execute("INSERT INTO order_intents(broker_id, account_id, portfolio_id, mode, idempotency_key, order_payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, storage_key, payload, "rejected" if reason else "accepted", now, now))
            order_id = int(cur.lastrowid)
            cash = self._cash_in_conn(conn, intent.currency, account_id, portfolio_id, broker_id)
            if reason:
                if audit_callback:
                    audit_callback(conn, "order.rejected", "order_intent", str(order_id), intent, {"reason": reason}, now)
                self._append_audit_event_conn(conn, "order.rejected", "order_intent", str(order_id), {"intent": intent.to_dict(), "reason": reason}, broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, now)
                return {"order_id": order_id, "status": "rejected", "reused": False, "cash": cash, "fill": None}
            is_buy = str(intent.side) == "buy"
            holding = conn.execute("SELECT shares, avg_cost FROM paper_holdings WHERE broker_id=? AND account_id=? AND portfolio=? AND symbol=?", (broker_id, account_id, portfolio_id, intent.symbol)).fetchone()
            owned = Decimal(holding["shares"]) if holding else Decimal("0")
            old_avg = Decimal(holding["avg_cost"]) if holding else Decimal("0")
            if max_position_weight is not None:
                current_notional = owned * fill_price
                projected_notional = current_notional + quantity * fill_price if is_buy else max(Decimal("0"), current_notional - quantity * fill_price)
                if portfolio_value is None:
                    raise ValueError("portfolio_value is required for position-weight check")
                portfolio_value = Decimal(str(portfolio_value))
                if portfolio_value <= 0:
                    raise ValueError("portfolio_value is required for position-weight check")
                if projected_notional / portfolio_value > max_position_weight:
                    reason = f"projected position weight exceeds limit: {projected_notional / portfolio_value} > {max_position_weight}"
                    conn.execute("UPDATE order_intents SET status='rejected', updated_at=? WHERE id=?", (now, order_id))
                    if audit_callback:
                        audit_callback(conn, "order.rejected", "order_intent", str(order_id), intent, {"reason": reason, "code": "max_position_weight"}, now)
                    self._append_audit_event_conn(conn, "order.rejected", "order_intent", str(order_id), {"intent": intent.to_dict(), "reason": reason, "code": "max_position_weight"}, broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, now)
                    return {"order_id": order_id, "status": "rejected", "reused": False, "cash": cash, "fill": None, "reason": reason}
            new_cash = cash - total if is_buy else cash + total
            balance_text = _canonical_decimal(new_cash, "balance", nonnegative=True)
            conn.execute("INSERT INTO cash_balances(broker_id, account_id, portfolio_id, currency, balance, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(broker_id, account_id, portfolio_id, currency) DO UPDATE SET balance=excluded.balance, updated_at=excluded.updated_at", (broker_id, account_id, portfolio_id, _currency(intent.currency), balance_text, now))
            amount = -total if is_buy else total
            conn.execute("INSERT INTO cash_ledger(broker_id, account_id, portfolio_id, currency, amount, balance_after, entry_type, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker_id, account_id, portfolio_id, _currency(intent.currency), _canonical_decimal(amount, "amount"), balance_text, "trade", str(order_id), now))
            new_shares = owned + quantity if is_buy else owned - quantity
            new_avg = ((owned * old_avg) + (quantity * fill_price) + fee) / new_shares if is_buy else (old_avg if new_shares else Decimal("0"))
            new_avg = new_avg.quantize(Decimal("0.000000000000000001"))
            shares_text, avg_text = _canonical_decimal(new_shares, "shares", nonnegative=True), _canonical_decimal(new_avg, "avg_cost", nonnegative=True)
            conn.execute("INSERT INTO paper_holdings(broker_id, account_id, portfolio, symbol, currency, shares, avg_cost, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(broker_id, account_id, portfolio, symbol) DO UPDATE SET currency=excluded.currency, shares=excluded.shares, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at", (broker_id, account_id, portfolio_id, intent.symbol, _currency(intent.currency), shares_text, avg_text, now))
            conn.execute("INSERT INTO paper_transactions(broker_id, account_id, portfolio, symbol, side, shares, price, currency, fees, trade_date, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker_id, account_id, portfolio_id, intent.symbol, str(intent.side), _canonical_decimal(quantity, "shares", positive=True), _canonical_decimal(fill_price, "price", positive=True), _currency(intent.currency), _canonical_decimal(fee, "fees", nonnegative=True), now[:10], "paper broker"))
            execution = {"fee": _canonical_decimal(fee, "fee", nonnegative=True), "total": _canonical_decimal(total, "total", nonnegative=True)}
            conn.execute("INSERT INTO fills(order_id, broker_id, account_id, external_fill_id, quantity, price, currency, execution_payload, filled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (order_id, broker_id, account_id, fill_id, _canonical_decimal(quantity, "fill quantity", positive=True), _canonical_decimal(fill_price, "fill price", positive=True), _currency(intent.currency), _canonical_json(execution, "execution payload"), now, now))
            conn.execute("UPDATE order_intents SET status='filled', updated_at=? WHERE id=?", (now, order_id))
            if audit_callback:
                audit_callback(conn, "order.filled", "order_intent", str(order_id), intent, {"fill_id": fill_id, "total": execution["total"]}, now)
            self._append_audit_event_conn(conn, "order.filled", "order_intent", str(order_id), {"intent": intent.to_dict(), "fill_id": fill_id, "total": execution["total"]}, broker_id, intent.account_id, intent.portfolio_id, intent.mode.value, now)
            return {"order_id": order_id, "status": "filled", "reused": False, "cash": new_cash, "fill": conn.execute("SELECT * FROM fills WHERE id=last_insert_rowid()").fetchone()}

    @staticmethod
    def _cash_in_conn(conn: sqlite3.Connection, currency: str, account_id: str, portfolio_id: str, broker_id: str) -> Decimal:
        row = conn.execute("SELECT balance FROM cash_balances WHERE broker_id=? AND account_id=? AND portfolio_id=? AND currency=?", (broker_id, account_id, portfolio_id, _currency(currency))).fetchone()
        return Decimal(row["balance"]) if row else Decimal("0")

    @staticmethod
    def _append_audit_event_conn(conn: sqlite3.Connection, event_type: str, entity_type: str, entity_id: str, payload: Any, broker_id: str | None, account_id: str | None, portfolio_id: str | None, mode: str | None, occurred_at: str) -> None:
        conn.execute("INSERT INTO audit_events(event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_type, entity_type, entity_id, broker_id, account_id, portfolio_id, mode, _canonical_json(payload, "audit payload"), occurred_at, occurred_at))

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
