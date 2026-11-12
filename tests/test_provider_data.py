from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_tracker.brokers import BrokerAdapterError, KiteAdapter, KiteConfig
from portfolio_tracker.db import Database
from portfolio_tracker.provider_data import ProviderSyncService, normalize_provider_snapshot


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "provider.sqlite3")
    db.init_db()
    return db


def test_provider_schema_normalizes_and_is_broker_scoped(tmp_path):
    db = _db(tmp_path)
    payload = {"data": {"holdings": [{"symbol": "INFY", "quantity": "2", "average_price": "100"}], "cash": [{"currency": "INR", "balance": "500"}]}}
    snapshot = normalize_provider_snapshot("zerodha_kite", "kite", "acct", payload)
    db.upsert_provider_snapshot(snapshot)
    other = normalize_provider_snapshot("upstox", "upstox", "acct", payload)
    db.upsert_provider_snapshot(other)
    assert len(db.list_provider_holdings(broker_id="kite", account_id="acct")) == 1
    assert len(db.list_provider_holdings(broker_id="upstox", account_id="acct")) == 1
    assert db.list_provider_cash(broker_id="kite", account_id="acct")[0]["balance"] == "500"


def test_sync_is_idempotent_and_does_not_touch_manual_or_paper(tmp_path):
    db = _db(tmp_path)
    db.upsert_holding("INFY", shares=7, avg_cost=90)
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"), transport=lambda op, payload: {"data": {"holdings": [{"symbol": "INFY", "quantity": "2", "average_price": "100"}]}})
    first = ProviderSyncService(db).sync(adapter)
    second = ProviderSyncService(db).sync(adapter)
    assert first["status"] == second["status"] == "succeeded"
    assert len(db.list_provider_holdings(broker_id="kite", account_id="acct")) == 1
    assert db.get_holding(portfolio="Main", symbol="INFY")["shares"] == 7
    assert first["reconciliation"]["manual_or_paper_mutated"] is False


def test_unconfigured_and_stale_connector_errors_are_safe(tmp_path):
    db = _db(tmp_path)
    unconfigured = KiteAdapter(KiteConfig(account_id="acct", api_key="key"))
    result = ProviderSyncService(db).sync(unconfigured)
    assert result["status"] == "error"
    assert result["error"]["code"] == "transport_unconfigured"
    expired = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="secret", token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)), transport=lambda op, payload: {})
    stale = ProviderSyncService(db).sync(expired)
    assert stale["status"] == "stale"
    assert stale["error"]["code"] == "token_expired"
    assert "secret" not in str(stale)


def test_provider_error_redacts_secret():
    def transport(operation, payload):
        return {"error": {"code": "provider_error", "message": "access_token=secret-value rejected"}}
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="secret-value"), transport=transport)
    with pytest.raises(BrokerAdapterError) as exc:
        adapter.snapshot()
    assert "secret-value" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
