"""Validated, local-only INDmoney US holdings importer."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from ..db import Database

INDMONEY_US_PORTFOLIO = "INDmoney-US"
_DEFAULT_PORTFOLIO = INDMONEY_US_PORTFOLIO
_REQUIRED = ("symbol", "shares", "average_cost")
_ALIASES = {"avg_cost": "average_cost", "average cost": "average_cost", "ticker": "symbol"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,14}$")

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

def _text(value: Any, field: str, required: bool = True) -> str:
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    return value

def _identifier(value: Any, field: str, default: str = "") -> str:
    value = _text(value, field, False) or default
    if value and not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} contains unsafe characters")
    return value

def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(_text(value, field))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a finite nonnegative decimal")
    sign, digits, exponent = number.as_tuple()
    if len(digits) > 38 or (isinstance(exponent, int) and abs(exponent) > 18) or len(format(number, "f")) > 64:
        raise ValueError(f"{field} is outside the supported numeric range")
    return number

def _timestamp(value: Any) -> str:
    value = _text(value, "imported_at", False)
    if not value:
        return datetime.now(timezone.utc).isoformat()
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("imported_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("imported_at must include a timezone")
    return value

def _portfolio(value: Any) -> str:
    result = _identifier(value, "portfolio", _DEFAULT_PORTFOLIO)
    if result != INDMONEY_US_PORTFOLIO:
        raise ValueError("INDmoney imports must target the dedicated INDmoney US portfolio")
    return result

def _normalize(record: Mapping[str, Any], *, source: str, portfolio: str | None = None) -> INDmoneyHolding:
    if not isinstance(record, Mapping):
        raise TypeError("each INDmoney record must be a mapping")
    data = {_ALIASES.get(str(k).strip().lower(), str(k).strip().lower()): v for k, v in record.items()}
    missing = [f for f in _REQUIRED if not _text(data.get(f), f, False)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    symbol = _text(data["symbol"], "symbol").upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("symbol contains unsafe characters")
    currency = _text(data.get("currency", "USD"), "currency").upper()
    if currency != "USD":
        raise ValueError("INDmoney US holdings must use currency USD")
    row_portfolio = _portfolio(portfolio if portfolio is not None else data.get("portfolio", _DEFAULT_PORTFOLIO))
    return INDmoneyHolding(symbol, _decimal(data["shares"], "shares"), _decimal(data["average_cost"], "average_cost"), currency,
        _identifier(data.get("label", ""), "label"), _identifier(data.get("account", ""), "account"), row_portfolio,
        source, _timestamp(data.get("imported_at", "")))

def _business_payload(row: INDmoneyHolding) -> dict[str, str]:
    return {"symbol": row.symbol, "shares": format(row.shares, "f"), "average_cost": format(row.average_cost, "f"),
            "currency": row.currency, "label": row.label, "account": row.account, "portfolio": row.portfolio, "source": row.source}

def _key(row: INDmoneyHolding) -> str:
    encoded = json.dumps(_business_payload(row), sort_keys=True, separators=(",", ":")).encode()
    return "indmoney-" + hashlib.sha256(encoded).hexdigest()

def parse_indmoney_csv(csv_input: str | TextIO) -> list[dict[str, str]]:
    text = csv_input.read() if isinstance(csv_input, io.TextIOBase) else csv_input
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV input is empty")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if not reader.fieldnames or any(not str(n).strip() for n in reader.fieldnames):
            raise ValueError("CSV must contain a non-empty header")
        headers = [str(n).strip().lower() for n in reader.fieldnames]
        canonical = [_ALIASES.get(n, n) for n in headers]
        if len(canonical) != len(set(canonical)):
            raise ValueError("CSV contains duplicate headers after alias normalization")
        if any(field not in canonical for field in _REQUIRED):
            raise ValueError("CSV is missing required columns: symbol, shares, average_cost")
        rows = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("CSV contains malformed or short rows")
            if not any(str(v).strip() for v in row.values()):
                raise ValueError("CSV contains a blank row")
            rows.append({canonical[i]: value for i, value in enumerate(row.values())})
        if not rows:
            raise ValueError("CSV contains no data rows")
        return rows
    except csv.Error as exc:
        raise ValueError("malformed CSV") from exc

def import_indmoney_csv(db: Database, csv_input: str | TextIO | Path, *, source: str | None = None, portfolio: str | None = None) -> ImportResult:
    if isinstance(csv_input, Path) or (isinstance(csv_input, str) and "\n" not in csv_input and Path(csv_input).is_file()):
        csv_input = Path(csv_input).read_text(encoding="utf-8")
    return import_indmoney_records(db, parse_indmoney_csv(csv_input), source=source, portfolio=portfolio)

def import_indmoney_records(db: Database, records: Iterable[Mapping[str, Any] | INDmoneyHolding], *, source: str | None = None, portfolio: str | None = None) -> ImportResult:
    if source is not None:
        source = _identifier(source, "source")
    if portfolio is not None:
        portfolio = _portfolio(portfolio)
    normalized = []
    for record in records:
        raw = asdict(record) if isinstance(record, INDmoneyHolding) else dict(record)
        effective_source = source if source is not None else (_text(raw.get("source"), "source", False) or "indmoney-manual")
        effective_source = _identifier(effective_source, "source")
        normalized.append(_normalize(raw, source=effective_source, portfolio=portfolio))
    if not normalized:
        raise ValueError("records must not be empty")
    portfolios = {row.portfolio for row in normalized}
    if len(portfolios) != 1:
        raise ValueError("one import may target only one portfolio")
    unique: dict[str, INDmoneyHolding] = {}
    symbols: dict[str, dict[str, str]] = {}
    for row in normalized:
        key, payload = _key(row), _business_payload(row)
        prior = symbols.get(row.symbol)
        if prior is not None and prior != payload:
            raise ValueError(f"conflicting duplicate row for {row.symbol} in {row.portfolio}")
        symbols[row.symbol] = payload
        unique[key] = row
    db.init_db()
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS indmoney_import_rows (
            import_key TEXT PRIMARY KEY, portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
            row_payload TEXT NOT NULL, imported_at TEXT NOT NULL, ingested_at TEXT NOT NULL, source TEXT NOT NULL
        )""")
        # Existing databases from the first importer need the provenance column.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(indmoney_import_rows)")}
        if "ingested_at" not in columns:
            conn.execute("ALTER TABLE indmoney_import_rows ADD COLUMN ingested_at TEXT NOT NULL DEFAULT ''")
        conn.execute("BEGIN IMMEDIATE")
        imported = skipped = 0
        accepted = []
        ingested_at = datetime.now(timezone.utc).isoformat()
        for key, row in sorted(unique.items()):
            payload = _business_payload(row)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            existing = conn.execute("SELECT row_payload FROM indmoney_import_rows WHERE import_key=?", (key,)).fetchone()
            if existing:
                if existing["row_payload"] != encoded:
                    raise ValueError(f"conflicting replay for import key {key}")
                skipped += 1; accepted.append(key); continue
            db.upsert_indmoney_holding_conn(conn, portfolio=row.portfolio, symbol=row.symbol, label=row.label, account=row.account, shares=row.shares, avg_cost=row.average_cost, source=row.source, source_imported_at=row.imported_at, ingested_at=ingested_at)
            conn.execute("INSERT INTO indmoney_import_rows(import_key,portfolio,symbol,row_payload,imported_at,ingested_at,source) VALUES(?,?,?,?,?,?,?)", (key, row.portfolio, row.symbol, encoded, row.imported_at, ingested_at, row.source))
            conn.execute("INSERT INTO audit_events(event_type,entity_type,entity_id,portfolio_id,payload,occurred_at,created_at) VALUES(?,?,?,?,?,?,?)", ("indmoney.imported", "indmoney_import_row", key, row.portfolio, json.dumps({**payload, "imported_at": row.imported_at, "ingested_at": ingested_at}, sort_keys=True, separators=(",", ":")), row.imported_at, ingested_at))
            imported += 1; accepted.append(key)
        return ImportResult(normalized[0].portfolio, imported, skipped, tuple(accepted))
