from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.brokers import (
    AdapterConfigurationError, BrokerAdapterError, KiteAdapter, KiteConfig,
    LiveExecutionDisabled, OrderStatus, UpstoxAdapter, UpstoxConfig,
)
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderType, TimeInForce


def intent(mode=ExecutionMode.LIVE):
    return OrderIntent("broker-order", "p1", "INFY", "buy", OrderType.MARKET, TimeInForce.DAY, "1", "INR", mode, account_id="acct")


def test_configs_require_auth_for_explicit_live_and_do_not_expose_secrets():
    with pytest.raises(AdapterConfigurationError):
        KiteConfig(account_id="acct", api_key="key", live_enabled=True)
    with pytest.raises(AdapterConfigurationError):
        UpstoxConfig(account_id="acct", client_id="client", live_enabled=True)
    config = KiteConfig(account_id="acct", api_key="key-secret", access_token="token-secret")
    assert "key-secret" not in repr(config) and "token-secret" not in repr(config)
    assert "key-secret" not in str(config.to_dict()) and "token-secret" not in str(config.to_dict())


def test_typed_intent_and_live_disabled_are_fail_closed():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"))
    with pytest.raises(TypeError):
        adapter.submit_order({"symbol": "INFY"})
    with pytest.raises(LiveExecutionDisabled) as error:
        adapter.submit_order(intent())
    assert error.value.code == "live_disabled"


def test_paper_intent_must_stay_on_gateway_even_when_adapter_has_transport():
    calls = []
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"), transport=lambda op, payload: calls.append(op) or {})
    with pytest.raises(BrokerAdapterError, match="ExecutionGateway/PaperBroker"):
        adapter.submit_order(intent(ExecutionMode.PAPER))
    assert calls == []


def test_symbol_mapping_and_normalized_provider_error():
    def transport(operation, payload):
        assert operation == "quote"
        assert payload["symbol"] == "NSE:INFY"
        return {"error": {"code": "rate_limit", "message": "too many requests", "status": 429}}

    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", exchange_symbols={"NSE:INFY": "NSE:INFY"}), transport=transport)
    with pytest.raises(BrokerAdapterError) as error:
        adapter.quote("INFY")
    assert error.value.provider == "zerodha_kite"
    assert error.value.code == "rate_limit"
    assert error.value.retryable


def test_upstox_live_transport_receives_typed_order_only_and_normalizes_status():
    seen = []

    def transport(operation, payload):
        seen.append((operation, payload))
        return {"data": {"order_id": "u-1", "status": "complete"}}

    adapter = UpstoxAdapter(UpstoxConfig(account_id="acct", client_id="client", access_token="token", live_enabled=True), transport=transport)
    receipt = adapter.submit_order(intent())
    assert receipt.status is OrderStatus.FILLED
    assert seen[0][1]["idempotency_key"] == "broker-order"
    assert "token" not in repr(adapter) and "client" not in repr(adapter)


def test_metadata_contains_scope_capabilities_and_token_expiry():
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", token_expires_at=expiry))
    public = adapter.metadata.to_dict()
    assert public["provider_name"] == "zerodha_kite"
    assert public["account_id"] == "acct"
    assert public["broker_id"] == "kite"
    assert "submit" in public["supported_capabilities"]
    assert public["token_expiry"]["expires_at"].startswith("2030-")
