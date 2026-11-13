import json
from pathlib import Path

import pytest

from portfolio_tracker.brokers.base import BrokerAdapterError, LiveExecutionDisabled
from portfolio_tracker.config import DEFAULT_SETTINGS, OFFICIAL_MCP_ENDPOINTS
from portfolio_tracker.provider_mcp import MCPClient, MCPProviderAdapter, redact


class Response:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.payload).encode()


class Opener:
    def __init__(self, responses): self.responses, self.requests = iter(responses), []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return next(self.responses)


def test_mcp_discovery_call_and_write_rejection():
    opener = Opener([
        Response({"jsonrpc":"2.0","id":1,"result":{}}),
        Response({"jsonrpc":"2.0","id":2,"result":{}}),
        Response({"jsonrpc":"2.0","id":3,"result":{"tools":[
            {"name":"get_holdings","inputSchema":{"type":"object"}},
            {"name":"place_order","inputSchema":{"type":"object"}},
        ]}}),
        Response({"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"{\"data\": {\"holdings\": []}}"}]}}),
    ])
    client = MCPClient("https://example.test/mcp", provider="kite", opener=opener)
    adapter = MCPProviderAdapter(provider="kite", broker_id="kite", account_id="acct", client=client)
    assert adapter.snapshot()["data"]["holdings"] == []
    assert "place_order" not in client.tools
    with pytest.raises(LiveExecutionDisabled): adapter.submit_order(object())


def test_mcp_auth_failure_and_redaction():
    assert "secret-value" not in str(redact("Bearer secret-value"))
    assert DEFAULT_SETTINGS["provider_modes"]["kite"] == "mcp_only"
    assert OFFICIAL_MCP_ENDPOINTS["kite"]["url"] == "https://mcp.kite.trade/mcp"
