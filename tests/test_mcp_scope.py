"""MCP portfolio-bound read tools must never widen an omitted token scope."""
import json
from decimal import Decimal

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.ledger import FinancialEvent
from portfolio_tracker.mcp_server import MCPServer


@pytest.fixture(scope="module")
def seeded_server(tmp_path_factory):
    db = Database(tmp_path_factory.mktemp("scoped-mcp") / "scoped-mcp.sqlite3")
    db.init_db()
    db.upsert_holding("PRIVATE", shares=2, avg_cost=10, portfolio="Private")
    db.upsert_holding("PUBLIC", shares=3, avg_cost=100, portfolio="Other")
    for portfolio, symbol in (("Private", "PRIVATE"), ("Other", "PUBLIC")):
        event_id = db.record_financial_event(FinancialEvent(
            event_type="buy", portfolio=portfolio, symbol=symbol,
            quantity=Decimal("1"), price=Decimal("10"), event_date="2026-01-01",
        ))
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tax_lots (event_id, namespace, portfolio, symbol, acquired_date, "
                "quantity, remaining_quantity, unit_cost, currency, created_at) "
                "VALUES (?, 'manual', ?, ?, '2026-01-01', '1', '1', '10', 'INR', '2026-01-01')",
                (event_id, portfolio, symbol),
            )
    return MCPServer(lambda: db)


@pytest.fixture
def scoped_server(seeded_server, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_MCP_PORTFOLIO", "Private")
    return seeded_server


def tool_call(server, name, arguments):
    return server.rpc({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


@pytest.mark.parametrize("name", ("holdings", "portfolio_summary", "ledger_tax_report"))
def test_scoped_read_rejects_explicit_other_portfolio(scoped_server, name):
    response = tool_call(scoped_server, name, {"portfolio": "Other"})
    assert response["error"]["message"] == "requested namespace is outside MCP token scope"


@pytest.mark.parametrize("name", ("holdings", "portfolio_summary", "ledger_tax_report"))
@pytest.mark.parametrize("arguments", ({}, {"portfolio": ""}, {"portfolio": "Private"}))
def test_scoped_read_defaults_to_only_permitted_portfolio(scoped_server, name, arguments):
    response = tool_call(scoped_server, name, arguments)
    assert "error" not in response
    result = json.loads(response["result"]["content"][0]["text"])
    if name == "holdings":
        assert [(row["portfolio"], row["symbol"]) for row in result["holdings"]] == [("Private", "PRIVATE")]
    elif name == "portfolio_summary":
        assert result == {"holdings_count": 1, "total_cost": "20.00"}
    else:
        assert [(row["portfolio"], row["symbol"]) for row in result["financial_events"]] == [("Private", "PRIVATE")]
        assert [(row["portfolio"], row["symbol"]) for row in result["tax_lots"]] == [("Private", "PRIVATE")]


def test_finance_goals_and_alerts_are_filtered_by_both_token_scope_dimensions(tmp_path, monkeypatch):
    db = Database(tmp_path / "finance-scope.sqlite3")
    db.init_db()
    with db.connect() as conn:
        for account, portfolio, name in (("acct-a", "port-a", "visible"), ("acct-b", "port-b", "hidden")):
            conn.execute(
                "INSERT INTO finance_goals(account_id, portfolio_id, name, target_amount, current_amount, currency, created_at, updated_at) "
                "VALUES (?, ?, ?, '100', '10', 'INR', 'now', 'now')",
                (account, portfolio, name),
            )
            conn.execute(
                "INSERT INTO alerts(namespace, account_id, portfolio_id, kind, severity, title, fingerprint, created_at, updated_at) "
                "VALUES ('manual', ?, ?, 'test', 'info', ?, ?, 'now', 'now')",
                (account, portfolio, name, f"fingerprint-{account}"),
            )
    monkeypatch.setenv("PORTFOLIO_MCP_ACCOUNT_ID", "acct-a")
    monkeypatch.setenv("PORTFOLIO_MCP_PORTFOLIO_ID", "port-a")
    server = MCPServer(lambda: db)

    response = tool_call(server, "finance_goals_alerts", {})

    assert "error" not in response
    result = json.loads(response["result"]["content"][0]["text"])
    assert [row["name"] for row in result["goals"]] == ["visible"]
    assert [row["title"] for row in result["alerts"]] == ["visible"]


@pytest.mark.parametrize(
    "scope_key,argument_key,other_value",
    (("PORTFOLIO_MCP_ACCOUNT_ID", "account_id", "acct-b"),
     ("PORTFOLIO_MCP_PORTFOLIO_ID", "portfolio_id", "port-b")),
)
def test_finance_goals_and_alerts_reject_explicit_other_scope(tmp_path, monkeypatch, scope_key, argument_key, other_value):
    db = Database(tmp_path / f"finance-mismatch-{argument_key}.sqlite3")
    db.init_db()
    monkeypatch.setenv(scope_key, "permitted")
    response = tool_call(MCPServer(lambda: db), "finance_goals_alerts", {argument_key: other_value})
    assert response["error"]["message"] == "requested namespace is outside MCP token scope"


def test_research_findings_are_advertised_as_global():
    from portfolio_tracker.mcp_server import TOOLS

    tool = next(item for item in TOOLS if item["name"] == "research_findings")
    assert tool["x-scope"] == "global"
    assert "global" in tool["description"].lower()
