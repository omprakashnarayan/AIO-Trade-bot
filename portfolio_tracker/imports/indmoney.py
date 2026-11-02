"""Safe, read-only INDmoney US holdings import.

This module accepts explicit CSV text/paths or manual records only. It never
logs in to, scrapes, calls, or places orders through INDmoney or any broker.
All writes are local, validated holdings snapshots with append-only provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from ..db import Database

_DEFAULT_PORTFOLIO = "INDmoney US"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,14}$")
_REQUIRED = ("symbol", "shares", "average_cost")
_ALIASES = {"avg_cost": "average_cost", "average cost": "average_cost", "ticker": "symbol"}


@dataclass(frozen=True)
class INDmoneyHolding:
    symbol: str
    shares: Decimal
    average_cost: Decimal
    currency: str = "USD"
    label: str = ""
    account: str = ""
    portfolio: str = _DEFAULT_PORTFOLIO
    source: str = "indmoney-manual"
    imported_at: str = ""


@dataclass(frozen=True)
class ImportResult:
    portfolio: str
    imported: int
    skipped: int
    row_keys: tuple[str, ...]


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    return value


def _identifier(value: Any, field: str, *, default: str = "") -> str:
    value = _text(value, field, required=False)
    if not value:
        return default
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} contains unsafe characters")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    text = _text(value, field)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a finite nonnegative decimal")
    exponent = number.as_tuple().exponent
    if len(number.as_tuple().digits) > 38 or (isinstance(exponent, int) and abs(exponent) > 18):
        raise ValueError(f"{field} is outside the supported numeric range")
    return number


def _timestamp(value: Any) -> str:
    value = _text(value, "imported_at", required=False)
    if not value:
        return datetime.now(timezone.utc).isoformat()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("imported_at must be an ISO-8601 timestamp") from exc
    return value


def _normalize(record: Mapping[str, Any]) -> INDmoneyHolding:
    if not isinstance(record, Mapping):
        raise TypeError("each INDmoney record must be a mapping")
    data = {_ALIASES.get(str(k).strip().lower(), str(k).strip().lower()): v for k, v in record.items()}
    missing = [field for field in _REQUIRED if _text(data.get(field), field, required=False) == ""]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    symbol = _text(data["symbol"], "symbol").upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("symbol contains unsafe characters")
    currency = _text(data.get("currency", "USD"), "currency").upper()
    if currency != "USD":
        raise ValueError("INDmoney US holdings must use currency USD")
    portfolio = _identifier(data.get("portfolio", _DEFAULT_PORTFOLIO), "portfolio", default=_DEFAULT_PORTFOLIO)
    return INDmoneyHolding(
        symbol=symbol,
        shares=_decimal(data["shares"], "shares"),
        average_cost=_decimal(data["average_cost"], "average_cost"),
        currency=currency,
        label=_identifier(data.get("label", ""), "label", default=""),
        account=_identifier(data.get("account", ""), "account", default=""),
        portfolio=portfolio,
        source=_identifier(data.get("source", "indmoney-manual"), "source", default="indmoney-manual"),
        imported_at=_timestamp(data.get("imported_at", "")),
    )


def _business_payload(row: INDmoneyHolding) -> dict[str, str]:
    return {
        "symbol": row.symbol, "shares": format(row.shares, "f"),
        "average_cost": format(row.average_cost, "f"), "currency": row.currency,
        "label": row.label, "account": row.account, "portfolio": row.portfolio,
        "source": row.source,
    }


def _key(row: INDmoneyHolding, ordinal: int | None) -> str:
    # imported_at is deliberately excluded so replaying the same source row at
    # a later time remains idempotent while the first timestamp is preserved.
    identity = {"ordinal": ordinal, **_business_payload(row)}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "indmoney-" + hashlib.sha256(encoded).hexdigest()


def parse_indmoney_csv(csv_input: str | TextIO) -> list[dict[str, str]]:
    """Parse strict CSV text/file-like input without any network access."""
    text = csv_input.read() if isinstance(csv_input, io.TextIOBase) else csv_input
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV input is empty")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if not reader.fieldnames or any(not str(name).strip() for name in reader.fieldnames):
            raise ValueError("CSV must contain a non-empty header")
        headers = [str(name).strip().lower() for name in reader.fieldnames]
        if len(headers) != len(set(headers)):
            raise ValueError("CSV contains duplicate headers")
        canonical = [_ALIASES.get(name, name) for name in headers]
        if any(field not in canonical for field in _REQUIRED):
            raise ValueError("CSV is missing required columns: symbol, shares, average_cost")
        rows: list[dict[str, str]] = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV contains malformed or short rows")
            if not any(str(value).strip() for value in row.values()):
                raise ValueError("CSV contains a blank row")
            rows.append({canonical[index]: value for index, value in enumerate(row.values())})
        if not rows:
            raise ValueError("CSV contains no data rows")
        return rows
    except csv.Error as exc:
        raise ValueError("malformed CSV") from exc


def import_indmoney_csv(db: Database, csv_input: str | TextIO | Path, *, source: str = "indmoney-csv") -> ImportResult:
    """Import a local CSV path/text into its declared or default US portfolio."""
    if isinstance(csv_input, Path):
        csv_input = csv_input.read_text(encoding="utf-8")
    elif isinstance(csv_input, str) and "\n" not in csv_input and Path(csv_input).is_file():
        csv_input = Path(csv_input).read_text(encoding="utf-8")
    return import_indmoney_records(db, parse_indmoney_csv(csv_input), source=source)


def import_indmoney_records(db: Database, records: Iterable[Mapping[str, Any] | INDmoneyHolding], *, source: str = "indmoney-manual") -> ImportResult:
    """Validate and idempotently import explicit manual holdings records.

    Existing unrelated portfolios are never touched. Exact duplicate rows in one
    import are ignored; conflicting duplicate symbols in one import are rejected.
    """
    source = _identifier(source, "source")
    normalized: list[INDmoneyHolding] = []
    for record in records:
        row = record if isinstance(record, INDmoneyHolding) else dict(record)
        if isinstance(row, dict) and not row.get("source"):
            row["source"] = source
        normalized.append(row if isinstance(row, INDmoneyHolding) else _normalize(row))
    if not normalized:
        raise ValueError("records must not be empty")
    portfolios = {row.portfolio for row in normalized}
    if len(portfolios) != 1:
        raise ValueError("one import may target only one portfolio")

    seen_symbols: dict[tuple[str, str], INDmoneyHolding] = {}
    keyed: list[tuple[str, INDmoneyHolding]] = []
    for ordinal, row in enumerate(normalized, 1):
        key = _key(row, ordinal)
        identity = (row.portfolio, row.symbol)
        previous = seen_symbols.get(identity)
        if previous is not None and _business_payload(previous) != _business_payload(row):
            raise ValueError(f"conflicting duplicate row for {row.symbol} in {row.portfolio}")
        seen_symbols[identity] = row
        keyed.append((key, row))

    db.init_db()
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS indmoney_import_rows (
            import_key TEXT PRIMARY KEY, portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
            row_payload TEXT NOT NULL, imported_at TEXT NOT NULL, source TEXT NOT NULL
        )""")
        conn.execute("BEGIN IMMEDIATE")
        imported = skipped = 0
        accepted_keys: list[str] = []
        for key, row in keyed:
            payload = _business_payload(row)
            existing = conn.execute("SELECT row_payload FROM indmoney_import_rows WHERE import_key=?", (key,)).fetchone()
            if existing:
                if existing["row_payload"] != json.dumps(payload, sort_keys=True, separators=(",", ":")):
                    raise ValueError(f"conflicting replay for import key {key}")
                skipped += 1
                accepted_keys.append(key)
                continue
            old = conn.execute("SELECT currency FROM holdings WHERE portfolio=? AND symbol=?", (row.portfolio, row.symbol)).fetchone()
            if old and old["currency"].upper() != "USD":
                raise ValueError(f"existing non-USD holding conflicts with {row.portfolio}/{row.symbol}")
            now = row.imported_at
            conn.execute("INSERT INTO holdings(portfolio,symbol,label,currency,shares,avg_cost,notes,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(portfolio,symbol) DO UPDATE SET label=excluded.label,currency=excluded.currency,shares=excluded.shares,avg_cost=excluded.avg_cost,notes=excluded.notes,updated_at=excluded.updated_at", (row.portfolio, row.symbol, row.label, "USD", float(row.shares), float(row.average_cost), f"INDmoney import; account={row.account}; source={row.source}", now))
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO indmoney_import_rows(import_key,portfolio,symbol,row_payload,imported_at,source) VALUES(?,?,?,?,?,?)", (key, row.portfolio, row.symbol, encoded, row.imported_at, row.source))
            conn.execute("INSERT INTO audit_events(event_type,entity_type,entity_id,portfolio_id,payload,occurred_at,created_at) VALUES(?,?,?,?,?,?,?)", ("indmoney.imported", "indmoney_import_row", key, row.portfolio, json.dumps({**payload, "imported_at": row.imported_at}, sort_keys=True, separators=(",", ":")), row.imported_at, now))
            imported += 1
            accepted_keys.append(key)
        return ImportResult(normalized[0].portfolio, imported, skipped, tuple(accepted_keys))
