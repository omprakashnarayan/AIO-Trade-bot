import json
import urllib.error
from email.message import Message
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
    assert OFFICIAL_MCP_ENDPOINTS["upstox"]["url"] == "https://mcp.upstox.com/mcp"


def test_authenticated_mcp_calls_use_provider_accepted_http_identity():
    class WAF:
        def open(self, request, timeout):
            if request.get_header("User-agent") != "Mozilla/5.0":
                raise urllib.error.HTTPError(request.full_url, 403, "rejected client", Message(), None)
            method = json.loads(request.data)["method"]
            result = {"initialize": {}, "tools/list": {"tools": [{"name": "get-holdings", "inputSchema": {"type": "object", "properties": {}}}]}}.get(method, {})
            return Response({"jsonrpc": "2.0", "id": 1, "result": result})
    client = MCPClient("https://mcp.upstox.com/mcp", provider="upstox", access_token="example-token", opener=WAF())
    assert "get-holdings" in client.discover()


def test_published_read_descriptions_do_not_look_like_write_actions():
    tools = [
        {"name": "get-holdings", "description": "Retrieve long-term equity holdings — stocks retained across previous trading sessions — with current value, quantity, average price, and P&L.", "inputSchema": {"$schema":"http://json-schema.org/draft-07/schema#", "type":"object", "properties":{}}},
        {"name": "get-positions", "description": "Retrieve current-day trading positions with real-time P&L, quantity, and margin details for all active intraday positions.", "inputSchema": {"$schema":"http://json-schema.org/draft-07/schema#", "type":"object", "properties":{}}},
    ]
    client, _ = _client_with_tools(tools)
    client.provider = "upstox"
    assert sorted(client.discover()) == ["get-holdings", "get-positions"]


def _client_with_tools(tools, *, response=None):
    opener = Opener([
        Response({"jsonrpc": "2.0", "id": 1, "result": {}}),
        Response({"jsonrpc": "2.0", "id": 2, "result": {}}),
        Response({"jsonrpc": "2.0", "id": 3, "result": {"tools": tools}}),
        Response({"jsonrpc": "2.0", "id": 4, "result": response or {"content": [{"type": "text", "text": "{\"holdings\": []}"}]}}),
    ])
    client = MCPClient("https://example.test/mcp", provider="kite", opener=opener)
    return client, opener


@pytest.mark.parametrize("name", ["buy", "sell", "cancel", "place_order", "get_orders", "snapshot_and_buy", "transfer_funds"])
def test_write_or_ambiguous_tools_never_verify_or_call(name):
    client, opener = _client_with_tools([{"name": name, "inputSchema": {"type": "object"}}])
    with pytest.raises(BrokerAdapterError) as exc:
        client.discover()
    assert exc.value.code == "mcp_no_read_tools"
    assert not client.verified
    assert len(opener.requests) == 3  # No tools/call request.
    with pytest.raises(BrokerAdapterError):
        client.call(name)
    assert len(opener.requests) == 3


def test_read_named_tool_with_write_description_is_not_verified():
    client, opener = _client_with_tools([{"name": "snapshot", "description": "Buy shares and place orders", "inputSchema": {"type": "object"}}])
    with pytest.raises(BrokerAdapterError):
        client.discover()
    assert not client.verified
    assert len(opener.requests) == 3


def test_snapshot_uses_only_discovered_schema_arguments():
    client, opener = _client_with_tools([{"name": "get_holdings", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}])
    adapter = MCPProviderAdapter(provider="kite", broker_id="kite", account_id="acct", client=client)
    assert adapter.snapshot() == {"holdings": []}
    assert json.loads(opener.requests[-1][0].data)["params"] == {"name": "get_holdings", "arguments": {}}


def test_snapshot_passes_account_id_when_discovered_schema_requires_it():
    schema = {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"], "additionalProperties": False}
    client, opener = _client_with_tools([{"name": "get_positions", "inputSchema": schema}])
    adapter = MCPProviderAdapter(provider="kite", broker_id="kite", account_id="acct", client=client)
    assert adapter.snapshot() == {"holdings": []}
    assert json.loads(opener.requests[-1][0].data)["params"] == {"name": "get_positions", "arguments": {"account_id": "acct"}}


def test_call_rejects_undiscovered_parameters_and_missing_required_without_rpc():
    schema = {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]}
    client, opener = _client_with_tools([{"name": "snapshot", "inputSchema": schema}])
    client.discover()
    for arguments in ({}, {"account_id": "acct", "action": "buy"}, {"account_id": 42}):
        with pytest.raises(BrokerAdapterError):
            client.call("snapshot", arguments)
    assert len(opener.requests) == 3


def test_snapshot_skips_incompatible_required_schema_and_uses_safe_alias():
    tools = [
        {"name": "snapshot", "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
        {"name": "positions", "inputSchema": {"type": "object", "properties": {}}},
    ]
    client, opener = _client_with_tools(tools)
    adapter = MCPProviderAdapter(provider="kite", broker_id="kite", account_id="acct", client=client)
    assert adapter.snapshot() == {"holdings": []}
    assert json.loads(opener.requests[-1][0].data)["params"]["name"] == "positions"


def test_only_incompatible_read_schema_cannot_verify():
    client, opener = _client_with_tools([{"name": "snapshot", "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}}])
    with pytest.raises(BrokerAdapterError) as exc:
        client.discover()
    assert exc.value.code == "mcp_no_read_tools"
    assert not client.verified
    assert len(opener.requests) == 3


def test_required_account_must_be_declared_in_properties():
    client, opener = _client_with_tools([{"name": "snapshot", "inputSchema": {"type": "object", "required": ["account_id"]}}])
    with pytest.raises(BrokerAdapterError) as exc:
        client.discover()
    assert exc.value.code == "mcp_no_read_tools"
    assert len(opener.requests) == 3


def test_no_argument_read_call_accepts_omitted_arguments():
    client, opener = _client_with_tools([{"name": "snapshot", "inputSchema": {"type": "object", "properties": {}}}])
    client.discover()
    assert client.call("snapshot") == {"holdings": []}
    assert json.loads(opener.requests[-1][0].data)["params"] == {"name": "snapshot", "arguments": {}}


def test_discovery_revokes_prior_read_when_server_only_exposes_write_tools():
    opener = Opener([
        Response({"jsonrpc": "2.0", "id": 1, "result": {}}),
        Response({"jsonrpc": "2.0", "id": 2, "result": {}}),
        Response({"jsonrpc": "2.0", "id": 3, "result": {"tools": [{"name": "snapshot", "inputSchema": {"type": "object"}}]}}),
        Response({"jsonrpc": "2.0", "id": 4, "result": {"tools": [{"name": "buy", "inputSchema": {"type": "object"}}]}}),
    ])
    client = MCPClient("https://example.test/mcp", provider="kite", opener=opener)
    assert client.discover_and_verify()["verified"]
    with pytest.raises(BrokerAdapterError):
        client.discover()
    assert not client.verified and not client.tools
    with pytest.raises(BrokerAdapterError):
        client.call("snapshot")
    assert len(opener.requests) == 4
