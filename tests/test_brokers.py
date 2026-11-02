from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.brokers import (
    AdapterConfigurationError, BrokerAdapterError, KiteAdapter, KiteConfig,
    LiveExecutionDisabled, UpstoxAdapter, UpstoxConfig,
)
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderType, TimeInForce


def intent(mode=ExecutionMode.LIVE, *, account_id="acct", order_type=OrderType.MARKET):
    return OrderIntent("broker-order", "p1", "INFY", "buy", order_type, TimeInForce.DAY, "1", "INR", mode,
                       account_id=account_id, limit_price="101" if order_type is OrderType.LIMIT else None,
                       stop_price="99" if order_type in {OrderType.STOP, OrderType.STOP_LIMIT} else None)



def test_configs_require_auth_for_explicit_live_and_do_not_expose_secrets():
    with pytest.raises(AdapterConfigurationError):
        KiteConfig(account_id="acct", api_key="key", live_enabled=True)
    with pytest.raises(AdapterConfigurationError):
        UpstoxConfig(account_id="acct", client_id="client", live_enabled=True)
    config = KiteConfig(account_id="acct", api_key="key-secret", access_token="token-secret")
    assert "key-secret" not in repr(config) and "token-secret" not in repr(config)
    assert "key-secret" not in str(config.to_dict()) and "token-secret" not in str(config.to_dict())


def test_configs_validate_timezone_and_exchange_symbol_mapping():
    with pytest.raises(AdapterConfigurationError, match="timezone-aware"):
        KiteConfig(account_id="acct", api_key="key", token_expires_at=datetime(2030, 1, 1))
    with pytest.raises(AdapterConfigurationError, match="EXCHANGE:SYMBOL"):
        KiteConfig(account_id="acct", api_key="key", exchange_symbols={"INFY": "NSE:INFY"})
    with pytest.raises(AdapterConfigurationError, match="values"):
        UpstoxConfig(account_id="acct", client_id="client", exchange_symbols={"NSE:INFY": 1})


def test_typed_intent_and_live_disabled_are_fail_closed():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"))
    with pytest.raises(TypeError):
        adapter.submit_order({"symbol": "INFY"})
    with pytest.raises(LiveExecutionDisabled) as error:
        adapter.submit_order(intent())
    assert error.value.code == "live_execution_not_enabled"
    assert str(error.value).endswith("live execution not enabled")


def test_direct_live_submit_and_cancel_are_rejected_without_transport_call():
    calls = []
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="token", live_enabled=True),
                          transport=lambda op, payload: calls.append((op, payload)) or {"data": {"order_id": "x"}})
    with pytest.raises(LiveExecutionDisabled) as submit_error:
        adapter.submit_order(intent())
    with pytest.raises(LiveExecutionDisabled) as cancel_error:
        adapter.cancel_order("x")
    assert submit_error.value.code == cancel_error.value.code == "live_execution_not_enabled"
    assert calls == []


def test_paper_intent_must_stay_on_gateway_even_when_adapter_has_transport():
    calls = []
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"), transport=lambda op, payload: calls.append(op) or {})
    with pytest.raises(BrokerAdapterError, match="ExecutionGateway/PaperBroker"):
        adapter.submit_order(intent(ExecutionMode.PAPER))
    assert calls == []


def test_account_scope_is_checked_before_live_disabled():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"))
    with pytest.raises(BrokerAdapterError) as error:
        adapter.submit_order(intent(account_id="other"))
    assert error.value.code == "account_mismatch"


def test_expired_token_is_checked_before_live_disabled():
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="token",
                                     token_expires_at=expired))
    with pytest.raises(BrokerAdapterError) as error:
        adapter.submit_order(intent())
    assert error.value.code == "token_expired"


def test_runtime_token_expiry_blocks_submit_and_cancel(monkeypatch):
    from portfolio_tracker.brokers import base

    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="token",
                                     token_expires_at=expiry))
    monkeypatch.setattr(base, "utc_now", lambda: expiry)
    with pytest.raises(BrokerAdapterError, match="expired"):
        adapter.submit_order(intent())
    with pytest.raises(BrokerAdapterError, match="expired"):
        adapter.cancel_order("x")
    with pytest.raises(BrokerAdapterError, match="expired"):
        adapter._call("submit_order", {}, write=True)


def test_cancel_requires_order_id_before_live_disabled():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"))
    with pytest.raises(BrokerAdapterError) as error:
        adapter.cancel_order("")
    assert error.value.code == "invalid_request"


def test_order_response_requires_order_id():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key"),
                          transport=lambda operation, payload: {"data": {"status": "open"}})
    with pytest.raises(BrokerAdapterError, match="no order ID") as error:
        adapter.order_status("x")
    assert error.value.code == "invalid_response"


def test_kite_payload_mapping_is_preserved_without_a_live_write_path():
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="token", live_enabled=True,
                                     exchange_symbols={"NSE:INFY": "NSE:INFY"}))
    payload = adapter._payload(intent(order_type=OrderType.LIMIT))
    for key in ("account_id", "portfolio_id", "symbol", "side", "quantity", "order_type", "limit_price",
                "stop_price", "currency", "asset_class", "time_in_force", "idempotency_key", "client_order_id"):
        assert key in payload


def test_symbol_mapping_and_redacted_provider_error():
    def transport(operation, payload):
        assert operation == "quote"
        assert payload["symbol"] == "NSE:INFY"
        return {"error": {"code": "rate_limit", "message": "token=secret-token too many requests", "status": 429}}

    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="secret-token",
                                     exchange_symbols={"NSE:INFY": "NSE:INFY"}), transport=transport)
    with pytest.raises(BrokerAdapterError) as error:
        adapter.quote("INFY")
    assert error.value.provider == "zerodha_kite"
    assert error.value.code == "rate_limit"
    assert error.value.retryable
    assert "secret-token" not in str(error.value)


def test_upstox_live_transport_is_rejected_and_provider_mapping_is_distinct():
    seen = []
    def transport(operation, payload):
        seen.append((operation, payload))
        return {"data": {"order_id": "u-1", "status": "complete"}}
    adapter = UpstoxAdapter(UpstoxConfig(account_id="acct", client_id="client", access_token="token", live_enabled=True,
                                         exchange_symbols={"NSE:INFY": "NSE_EQ|INE009A01021"}), transport=transport)
    with pytest.raises(LiveExecutionDisabled):
        adapter.submit_order(intent())
    payload = adapter._payload(intent())
    assert payload["instrument_token"] == "NSE_EQ|INE009A01021"
    assert payload["transaction_type"] == "BUY"
    assert payload["validity"] == "DAY"
    assert seen == []
    assert "token" not in repr(adapter) and "client" not in repr(adapter)


def test_no_capability_can_authorize_a_live_transport_call():
    calls = []
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token="token", live_enabled=True),
                          transport=lambda op, payload: calls.append(op) or {"data": {"order_id": "x"}})
    from portfolio_tracker.brokers import base

    assert not hasattr(adapter, "_bind_execution_capability")
    assert not hasattr(adapter, "_execution_capability")
    assert not hasattr(base, "_ExecutionCapability")
    assert not hasattr(base, "_mint_execution_capability")
    assert not hasattr(adapter, "_submit_order")
    assert not hasattr(adapter, "_cancel_order")
    with pytest.raises(LiveExecutionDisabled):
        adapter.submit_order(intent())
    with pytest.raises(LiveExecutionDisabled):
        adapter.cancel_order("x")
    with pytest.raises(LiveExecutionDisabled):
        adapter._call("submit_order", {}, write=True)
    assert calls == []


def test_transport_exception_chain_does_not_retain_secret():
    secret = "secret-token"

    def transport(operation, payload):
        raise RuntimeError(f"provider rejected bearer {secret}")

    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", access_token=secret), transport=transport)
    with pytest.raises(BrokerAdapterError) as error:
        adapter.quote("INFY")
    assert error.value.__cause__ is None
    assert secret not in repr(error.value)


def test_metadata_contains_scope_capabilities_and_token_expiry():
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    adapter = KiteAdapter(KiteConfig(account_id="acct", api_key="key", token_expires_at=expiry))
    public = adapter.metadata.to_dict()
    assert public["provider_name"] == "zerodha_kite"
    assert public["account_id"] == "acct"
    assert public["broker_id"] == "kite"
    assert "submit" in public["supported_capabilities"]
    assert public["token_expiry"]["expires_at"].startswith("2030-")
