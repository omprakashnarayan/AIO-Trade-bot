from decimal import Decimal
import json

from portfolio_tracker.db import Database
from portfolio_tracker.finance_ops import (
    acknowledge_alert, add_goal_contribution, backup_payload, create_alert, create_goal,
    correct_transaction, forecast_goal, goal_progress, list_goals, restore_backup, set_alert_delivery,
    write_backup,
)


def db(tmp_path):
    value = Database(tmp_path / "finance.sqlite3")
    value.init_db()
    return value


def test_decimal_progress_and_forecast():
    progress = goal_progress("1000", "250", start="0")
    assert progress["percent"] == "25"
    forecast = forecast_goal("1000", "250", "250", annual_return="0")
    assert forecast["months"] == 3
    assert forecast["projected_value"] == "1000"


def test_goal_contribution_is_durable(tmp_path):
    database = db(tmp_path)
    goal_id = create_goal(database, {"name": "Rainy day", "target_amount": "1000", "starting_amount": "100", "monthly_contribution": "100"})
    add_goal_contribution(database, goal_id, Decimal("50.25"), provenance={"source_id": "manual-1"})
    assert list_goals(database)[0]["current_amount"] == "150.25"


def test_alert_deduplication_ack_and_delivery(tmp_path):
    database = db(tmp_path)
    first = create_alert(database, {"kind": "stale_quotes", "title": "Stale", "fingerprint": "same"})
    assert create_alert(database, {"kind": "stale_quotes", "title": "Stale", "fingerprint": "same"}) == first
    assert acknowledge_alert(database, first, actor="tester")
    set_alert_delivery(database, first, "terminal", read=True)
    row = database.connect().execute("SELECT state, read_at FROM alert_deliveries WHERE alert_id=?", (first,)).fetchone()
    assert row["state"] == "delivered" and row["read_at"]


def test_backup_integrity_and_redaction(tmp_path):
    database = db(tmp_path)
    create_alert(database, {"kind": "oauth_expiry", "title": "Token", "details": {"access_token": "secret"}, "fingerprint": "redact"})
    path = tmp_path / "backup.json"
    result = write_backup(database, path)
    assert result["sha256"]
    assert "secret" not in path.read_text()
    restored = Database(tmp_path / "restored.sqlite3")
    restored.init_db()
    restore_backup(restored, path)


def test_correction_audit_trail(tmp_path):
    database = db(tmp_path)
    database.adjust_holding("INFY", 1, 100, "buy")
    transaction_id = database.list_transactions()[0]["id"]
    correction_id = correct_transaction(database, transaction_id, {"price": "101", "reason": "typo"}, actor="tester")
    row = database.connect().execute("SELECT * FROM transaction_corrections WHERE id=?", (correction_id,)).fetchone()
    assert row["transaction_id"] == transaction_id and json.loads(row["original_json"])["price"] == 100.0


def test_goal_namespace_isolation(tmp_path):
    database = db(tmp_path)
    create_goal(database, {"name": "Manual", "target_amount": "10", "namespace": "manual"})
    create_goal(database, {"name": "Broker", "target_amount": "10", "namespace": "broker"})
    assert [x["name"] for x in list_goals(database, namespace="manual")] == ["Manual"]
