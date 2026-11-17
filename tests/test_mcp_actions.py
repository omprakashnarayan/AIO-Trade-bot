"""Safe local mutation tools exposed by the application MCP server."""
import json

import pytest

from portfolio_tracker import config
from portfolio_tracker import web
from portfolio_tracker.db import Database
from portfolio_tracker.mcp_server import MCPServer, TOOLS, redact


@pytest.fixture
def action_server(tmp_path, monkeypatch):
    db = Database(tmp_path / "actions.sqlite3")
    db.init_db()
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "app" / "settings.json")
    return MCPServer(lambda: db), db


def rpc(server, name, arguments):
    return server.rpc({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def result(response):
    assert "error" not in response
    return json.loads(response["result"]["content"][0]["text"])


def test_action_tools_are_explicitly_mutating_and_have_no_live_or_provider_writes():
    tools = {item["name"]: item for item in TOOLS}
    expected = {
        "paper_proposal_create", "paper_order_submit", "kill_switch_set",
        "scheduler_set_enabled", "agent_set_enabled", "agent_run",
    }
    assert expected <= tools.keys()
    assert all(tools[name].get("x-mutating") for name in expected)
    assert not any("live" in name.lower() or "broker" in name.lower() for name in tools if tools[name].get("x-mutating"))
    assert "mode" not in tools["paper_order_submit"]["inputSchema"]["properties"]


def test_paper_order_uses_configured_mcp_scope_as_defaults(action_server, monkeypatch):
    server, db = action_server
    monkeypatch.setenv("PORTFOLIO_MCP_ACCOUNT_ID", "mcp-account")
    monkeypatch.setenv("PORTFOLIO_MCP_PORTFOLIO", "mcp-portfolio")
    db.upsert_cash_balance(balance="1000", currency="INR", account_id="mcp-account", portfolio_id="mcp-portfolio", broker_id="paper")

    submitted = result(rpc(server, "paper_order_submit", {
        "idempotency_key": "mcp-paper-buy", "symbol": "INFY", "side": "buy",
        "quantity": "2", "quote_price": "100",
    }))

    assert submitted["status"] == "submitted"
    order = db.list_order_intents()[0]
    assert (order["mode"], order["broker_id"], order["account_id"], order["portfolio_id"]) == (
        "paper", "paper", "mcp-account", "mcp-portfolio",
    )
    assert len(db.list_fills()) == 1
    sold = result(rpc(server, "paper_order_submit", {
        "idempotency_key": "mcp-paper-sell", "symbol": "INFY", "side": "sell",
        "quantity": "1", "quote_price": "120",
    }))
    assert sold["status"] == "submitted"
    assert len(db.list_fills()) == 2


def test_paper_order_uses_ui_settings_defaults_without_token_scope(action_server, monkeypatch):
    server, db = action_server
    monkeypatch.delenv("PORTFOLIO_MCP_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PORTFOLIO_MCP_PORTFOLIO_ID", raising=False)
    monkeypatch.delenv("PORTFOLIO_MCP_PORTFOLIO", raising=False)
    config.save_settings({
        **config.load_settings(),
        "default_account_id": "ui-account",
        "default_portfolio_id": "ui-portfolio",
    })
    db.upsert_cash_balance(balance="1000", currency="INR", account_id="ui-account", portfolio_id="ui-portfolio", broker_id="paper")

    response = rpc(server, "paper_order_submit", {
        "idempotency_key": "ui-scope-order", "symbol": "INFY", "side": "buy",
        "quantity": "1", "quote_price": "10",
    })

    assert result(response)["status"] == "submitted"
    order = db.list_order_intents()[0]
    assert (order["account_id"], order["portfolio_id"]) == ("ui-account", "ui-portfolio")


@pytest.mark.parametrize("tool,arguments", (
    ("paper_order_submit", {"portfolio_id": "other-portfolio"}),
    ("finance_goals_alerts", {"portfolio_id": "other-portfolio"}),
    ("holdings", {"portfolio": "other-portfolio"}),
))
def test_ui_default_scope_rejects_explicit_other_portfolio(action_server, monkeypatch, tool, arguments):
    server, db = action_server
    for key in ("PORTFOLIO_MCP_ACCOUNT_ID", "PORTFOLIO_MCP_PORTFOLIO_ID", "PORTFOLIO_MCP_PORTFOLIO"):
        monkeypatch.delenv(key, raising=False)
    config.save_settings({**config.load_settings(), "default_account_id": "ui-account", "default_portfolio_id": "ui-portfolio"})
    payload = {"idempotency_key": "wrong-ui-scope", "symbol": "INFY", "side": "buy", "quantity": "1", "quote_price": "10", **arguments}

    response = rpc(server, tool, payload)

    assert response["error"]["message"] == "requested namespace is outside MCP token scope"
    assert db.list_order_intents() == []


@pytest.mark.parametrize("unsupported", ({"order_type": "limit"}, {"limit_price": "9"}))
def test_mcp_paper_orders_reject_limit_fields_at_boundary(action_server, unsupported):
    server, db = action_server
    response = rpc(server, "paper_order_submit", {
        "idempotency_key": "unsupported-limit", "symbol": "INFY", "side": "buy",
        "quantity": "1", "quote_price": "10", **unsupported,
    })

    assert response["error"]["message"] == "MCP paper orders support market orders only"
    assert db.list_order_intents() == []
    tool = next(item for item in TOOLS if item["name"] == "paper_order_submit")
    assert tool["inputSchema"]["properties"]["order_type"]["enum"] == ["market"]
    assert "limit_price" not in tool["inputSchema"]["properties"]


def test_mcp_redacts_nested_secret_keys_and_serialized_json():
    value = {
        "access_token": "top-secret",
        "ordinary": "keep-me",
        "nested": [{"Refresh-Token": "refresh-secret", "label": "visible"}],
        "serialized": '{"api_key":"api-secret","authorization":"Bearer auth-secret","note":"ordinary"}',
    }

    redacted = redact(value)

    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["ordinary"] == "keep-me"
    assert redacted["nested"] == [{"Refresh-Token": "[REDACTED]", "label": "visible"}]
    serialized = json.loads(redacted["serialized"])
    assert serialized == {"api_key": "[REDACTED]", "authorization": "[REDACTED]", "note": "ordinary"}


def test_mcp_redacts_secret_key_suffix_variants():
    secret = "sensitive-test-value"
    result = redact({"secretKey": secret, "apiSecretKey": secret,
                     "nested": '{"secretKey":"sensitive-test-value"}', "label": "safe"})
    assert result["secretKey"] == "[REDACTED]"
    assert result["apiSecretKey"] == "[REDACTED]"
    assert json.loads(result["nested"])["secretKey"] == "[REDACTED]"
    assert result["label"] == "safe"


def test_mcp_redacts_secret_key_assignments_inside_plain_text():
    for key in ("secretKey", "apiSecretKey", "api_secret_key"):
        result = redact({"message": f"upstream {key}=FIXTURE_SENTINEL_DO_NOT_USE, retry"})
        assert "FIXTURE_SENTINEL_DO_NOT_USE" not in result["message"]
        assert "[REDACTED]" in result["message"]


@pytest.mark.parametrize(
    "scope,provided",
    (("PORTFOLIO_MCP_ACCOUNT_ID", "other-account"), ("PORTFOLIO_MCP_PORTFOLIO", "other-portfolio")),
)
def test_paper_order_rejects_mismatched_explicit_mcp_scope(action_server, monkeypatch, scope, provided):
    server, db = action_server
    monkeypatch.setenv(scope, "permitted")
    arguments = {
        "idempotency_key": "out-of-scope", "symbol": "INFY", "side": "buy",
        "quantity": "1", "quote_price": "10",
        "account_id": "permitted", "portfolio_id": "permitted",
    }
    arguments["account_id" if scope.endswith("ACCOUNT_ID") else "portfolio_id"] = provided

    response = rpc(server, "paper_order_submit", arguments)

    assert response["error"]["message"] == "requested namespace is outside MCP token scope"
    assert db.list_order_intents() == []
    assert db.list_fills() == []


def test_kill_switch_only_accepts_on_or_off_and_audits_changes(action_server):
    server, db = action_server

    enabled = result(rpc(server, "kill_switch_set", {"state": "on"}))
    assert enabled == {"global_kill_switch": True}
    assert db.get_console_state("global_kill_switch") == "on"
    assert rpc(server, "kill_switch_set", {"state": "enabled"})["error"]
    disabled = result(rpc(server, "kill_switch_set", {"state": "off"}))
    assert disabled == {"global_kill_switch": False}
    events = db.list_audit_events(limit=10)
    assert [event["event_type"] for event in events].count("risk.kill_switch.changed") == 2


def test_kill_switch_state_rolls_back_when_audit_insert_fails(action_server):
    server, db = action_server
    db.set_console_state("global_kill_switch", "off")
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_kill_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type='risk.kill_switch.changed' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )

    response = rpc(server, "kill_switch_set", {"state": "on"})

    assert "error" in response
    assert db.get_console_state("global_kill_switch") == "off"


def test_agent_enabled_state_rolls_back_when_audit_insert_fails(action_server):
    server, db = action_server
    assert db.get_agent("paper_trading")["enabled"] == 1
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_agent_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type='mcp.agent.enabled' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )

    response = rpc(server, "agent_set_enabled", {"name": "paper_trading", "enabled": False})

    assert "error" in response
    assert db.get_agent("paper_trading")["enabled"] == 1


def test_scheduler_setting_is_unchanged_when_audit_insert_fails(action_server):
    server, db = action_server
    original = config.load_settings()["scheduler_enabled"]
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_scheduler_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type='scheduler.state.changed' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )

    response = rpc(server, "scheduler_set_enabled", {"enabled": not original})

    assert "error" in response
    assert config.load_settings()["scheduler_enabled"] is original


def test_scheduler_save_failure_is_audited_as_request_not_change(action_server, monkeypatch):
    from portfolio_tracker import mcp_server

    server, db = action_server
    original = config.load_settings()["scheduler_enabled"]

    def fail_save(_settings):
        raise OSError("injected settings write failure")

    monkeypatch.setattr(mcp_server, "save_settings", fail_save)
    response = rpc(server, "scheduler_set_enabled", {"enabled": not original})

    assert "error" in response
    assert config.load_settings()["scheduler_enabled"] is original
    assert [event["event_type"] for event in db.list_audit_events()] == ["scheduler.state.change_requested"]


def test_kill_switch_blocks_paper_order_submission(action_server):
    server, db = action_server
    db.upsert_cash_balance(balance="1000", currency="INR", account_id="", portfolio_id="Main", broker_id="paper")
    result(rpc(server, "kill_switch_set", {"state": "on"}))

    submitted = result(rpc(server, "paper_order_submit", {
        "idempotency_key": "killed-order", "symbol": "INFY", "side": "buy",
        "quantity": "1", "quote_price": "10",
    }))

    assert submitted["status"] == "rejected"
    assert submitted["submitted"] is False
    assert submitted["risk"] == "global kill switch is active"


def test_scheduler_toggle_changes_only_local_setting_and_is_audited(action_server):
    server, db = action_server

    enabled = result(rpc(server, "scheduler_set_enabled", {"enabled": True}))

    assert enabled == {"enabled": True}
    assert config.load_settings()["scheduler_enabled"] is True
    assert db.get_console_state("scheduler_enabled", "unset") == "unset"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheduler_state").fetchone()[0] == 0
    event_types = [event["event_type"] for event in db.list_audit_events(limit=5)]
    assert event_types == ["scheduler.state.change_requested", "scheduler.state.changed"]
    assert rpc(server, "scheduler_set_enabled", {"enabled": "true"})["error"]


def test_allowlisted_agents_can_be_enabled_disabled_and_run_locally(action_server):
    server, db = action_server

    disabled = result(rpc(server, "agent_set_enabled", {"name": "paper_trading", "enabled": False}))
    assert disabled["enabled"] is False
    assert rpc(server, "agent_run", {"name": "paper_trading"})["error"]["message"] == "agent is disabled"
    assert result(rpc(server, "agent_set_enabled", {"name": "paper_trading", "enabled": True}))["enabled"] is True

    run = result(rpc(server, "agent_run", {"name": "paper_trading"}))

    assert run["agent_name"] == "paper_trading"
    assert run["status"] == "succeeded"
    assert run["result"]["paper_only"] is True
    assert run["result"]["live_writes"] is False
    assert db.list_audit_events(limit=10)[-1]["event_type"] == "mcp.agent.run"
    assert rpc(server, "agent_set_enabled", {"name": "not-allowlisted", "enabled": True})["error"]
    assert rpc(server, "agent_run", {"name": "not-allowlisted"})["error"]


def test_scheduler_and_agent_mutations_do_not_accept_ambiguous_values(action_server):
    server, _ = action_server
    assert rpc(server, "agent_set_enabled", {"name": "risk_review", "enabled": "false"})["error"]
    assert rpc(server, "scheduler_set_enabled", {"enabled": 1})["error"]


def test_bearer_authenticated_mcp_mutation_does_not_require_csrf(action_server, monkeypatch):
    server, db = action_server
    monkeypatch.setenv("PORTFOLIO_MCP_API_TOKEN", "local-mcp-token")
    monkeypatch.setattr(web, "MCPServer", lambda: server)

    response = web.app.test_client().post(
        "/mcp",
        headers={"Authorization": "Bearer local-mcp-token"},
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "kill_switch_set", "arguments": {"state": "on"}},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["result"]["content"][0]["text"] == '{"global_kill_switch":true}'
    assert db.get_console_state("global_kill_switch") == "on"


def test_mcp_endpoint_rejects_requests_without_a_token_or_operator_session(monkeypatch):
    monkeypatch.delenv("PORTFOLIO_MCP_API_TOKEN", raising=False)

    response = web.app.test_client().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["message"] == "MCP authentication required"
