import json
import sqlite3
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce


def make_intent(key="intent-001"):
    return OrderIntent(
        idempotency_key=key,
        portfolio_id="india-short-term",
        account_id="kite-account",
        symbol="INFY",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        currency="INR",
        mode=ExecutionMode.PAPER,
        limit_price=Decimal("1500.25"),
    )


def test_schema_initialization_is_idempotent(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    db.init_db()

    with db.connect() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"order_intents", "fills", "cash_ledger", "cash_balances", "audit_events"} <= tables


def test_order_intent_insert_and_read_preserves_serialized_payload(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()

    order_id = db.insert_order_intent(make_intent(), broker_id="kite")
    row = db.get_order_intent(order_id)

    assert row["id"] == order_id
    assert row["broker_id"] == "kite"
    assert row["account_id"] == "kite-account"
    assert row["portfolio_id"] == "india-short-term"
    assert row["mode"] == "paper"
    assert row["status"] == "pending"
    assert row["idempotency_key"] == "intent-001"
    assert json.loads(row["order_payload"])["quantity"] == "2"


def test_duplicate_idempotency_key_is_rejected_and_transaction_rolls_back(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    db.insert_order_intent(make_intent())

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_order_intent(make_intent())

    assert len(db.list_order_intents()) == 1


def test_audit_events_append_and_read_in_order(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()

    first_id = db.append_audit_event(
        event_type="order.created",
        entity_type="order_intent",
        entity_id="1",
        payload={"status": "pending"},
        mode="paper",
        account_id="kite-account",
    )
    second_id = db.append_audit_event(
        event_type="order.accepted",
        entity_type="order_intent",
        entity_id="1",
        payload={"status": "accepted"},
        mode="paper",
        account_id="kite-account",
    )

    rows = db.list_audit_events(entity_id="1")
    assert [row["id"] for row in rows] == [first_id, second_id]
    assert json.loads(rows[0]["payload"])["status"] == "pending"
    assert rows[1]["event_type"] == "order.accepted"


def test_connection_context_rolls_back_failed_write(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()

    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO audit_events(event_type, entity_type, entity_id, payload, occurred_at) VALUES (?, ?, ?, ?, ?)",
                ("test", "entity", "one", "{}", "2026-11-02T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO audit_events(event_type, entity_type, entity_id, payload, occurred_at) VALUES (?, ?, ?, ?, ?)",
                ("test", "entity", "one", "{}", "2026-11-02T00:00:00+00:00"),
            )

    assert db.list_audit_events() == []
