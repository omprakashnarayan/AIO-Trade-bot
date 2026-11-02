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
                "INSERT INTO audit_events(event_type, entity_type, entity_id, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("test", "entity", "one", "{}", "2026-11-02T00:00:00+00:00", "2026-11-02T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO audit_events(event_type, entity_type, entity_id, mode, payload, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("test", "entity", "one", "bogus", "{}", "2026-11-02T00:00:00+00:00", "2026-11-02T00:00:00+00:00"),
            )

    assert db.list_audit_events() == []


def test_fill_insert_read_and_duplicate_replay_is_rejected(tmp_path):
    from datetime import datetime, timezone
    from portfolio_tracker.trading_models import Fill

    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    order_id = db.insert_order_intent(make_intent(), broker_id="kite")
    fill = Fill("fill-001", "intent-001", Decimal("1.25"), Decimal("1501.50"), "INR", datetime.now(timezone.utc))

    fill_id = db.insert_fill(fill, order_id=order_id, execution_payload={"venue": "NSE"})
    row = db.get_fill(fill_id)
    assert row["quantity"] == "1.25"
    assert row["price"] == "1501.50"
    assert json.loads(row["execution_payload"]) == {"venue": "NSE"}
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_fill(fill, order_id=order_id)
    assert len(db.list_fills(order_id)) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", True, "1e100"])
def test_financial_values_are_validated_before_persistence(tmp_path, value):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    with pytest.raises((TypeError, ValueError)):
        db.insert_cash_ledger(amount=value, currency="INR", entry_type="deposit", account_id="acct")


def test_cash_ledger_and_balance_use_canonical_decimal_text_and_nonnull_scope(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    entry_id = db.insert_cash_ledger(amount=Decimal("1.2300"), balance_after=Decimal("9.00"), currency="inr", entry_type="deposit", portfolio_id=" p1 ")
    db.upsert_cash_balance(balance=Decimal("9.00"), currency="inr", portfolio_id=" p1 ")
    assert db.get_cash_ledger(entry_id)["amount"] == "1.2300"
    assert db.get_cash_balance(currency="INR", portfolio_id="p1")["balance"] == "9.00"
    with db.connect() as conn:
        assert conn.execute("SELECT account_id, portfolio_id FROM cash_balances").fetchone()["account_id"] == ""


def test_invalid_order_status_and_audit_json_are_rejected(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    with pytest.raises(ValueError):
        db.insert_order_intent(make_intent(), status="bogus")
    with pytest.raises(ValueError):
        db.append_audit_event(event_type="x", entity_type="y", entity_id="z", payload="not-json")


def test_adjust_holding_validates_values_and_serializes_writes(tmp_path):
    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()
    db.adjust_holding("INFY", Decimal("2"), Decimal("100"), "buy", fees=Decimal("1"))
    with pytest.raises(ValueError):
        db.adjust_holding("INFY", Decimal("0"), Decimal("100"), "buy")
    with pytest.raises(ValueError):
        db.adjust_holding("INFY", Decimal("1"), Decimal("100"), "buy", fees=Decimal("-1"))
    assert db.list_holdings()[0]["shares"] == 2.0


def test_adjust_holding_serializes_concurrent_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = Database(tmp_path / "trading.sqlite3")
    db.init_db()

    def buy_one():
        db.adjust_holding("INFY", Decimal("1"), Decimal("100"), "buy")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: buy_one(), range(2)))

    assert db.list_holdings()[0]["shares"] == 2.0
