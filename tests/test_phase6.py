from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.execution import ExecutionGateway, LiveModeRejected
from portfolio_tracker.mcp_server import MCPServer
from portfolio_tracker.provider_mcp import VerifiedMCPTransport
from portfolio_tracker.promotion_gates import GATE_DEFINITIONS, PromotionGates
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce


def test_all_promotion_gates_are_durable_and_require_pass(tmp_path):
    db = Database(tmp_path / "phase6.sqlite3"); db.init_db()
    gates = PromotionGates(db)
    assert gates.status()["ready"] is False
    for key, _ in GATE_DEFINITIONS:
        gates.record(key, status="pass", evidence="verified", reviewer="reviewer")
    assert gates.status()["ready"] is True
    assert PromotionGates(db).status()["passed"] == len(GATE_DEFINITIONS)
    assert any(dict(row)["event_type"] == "promotion_gate.recorded" for row in db.list_audit_events())


def test_live_rejected_independently_of_gate_state(tmp_path):
    db = Database(tmp_path / "phase6.sqlite3"); db.init_db()
    gates = PromotionGates(db)
    for key, _ in GATE_DEFINITIONS: gates.record(key, status="pass", evidence="x", reviewer="r")
    intent = OrderIntent("live-key", "Main", "INFY", OrderSide.BUY, OrderType.MARKET, TimeInForce.DAY, Decimal("1"), "INR", ExecutionMode.LIVE)
    with pytest.raises(LiveModeRejected): ExecutionGateway(db).submit(intent, quote_price=100)


def test_mcp_initialize_tools_call_and_redaction(tmp_path, monkeypatch):
    db = Database(tmp_path / "mcp.sqlite3")
    server = MCPServer(lambda: db)
    assert server.rpc({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})["result"]["serverInfo"]["name"] == "portfolio-tracker"
    tools = server.rpc({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})["result"]["tools"]
    names = {item["name"] for item in tools}
    assert {"health_status", "paper_order_submit", "promotion_gate_status"} <= names
    response = server.rpc({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"provider_integration_status","arguments":{}}})
    assert "unconfigured" in response["result"]["content"][0]["text"]
    assert "live" not in names


def test_provider_transport_requires_discovery_and_schema():
    transport = VerifiedMCPTransport(lambda: [{"name":"snapshot","inputSchema":{"type":"object"}}], lambda name, payload: {"data": {}})
    assert transport.discover_and_verify()["verified"] is True
    assert transport("snapshot", {}) == {"data": {}}
