"""Session mutations require a real CSRF token; bearer MCP stays usable."""
import re

import pytest
from werkzeug.security import generate_password_hash

from portfolio_tracker import config, web
from portfolio_tracker.db import Database
from portfolio_tracker.mcp_server import MCPServer


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PORTFOLIO_TRACKER_HOME", str(home))
    monkeypatch.setattr(config, "APP_DIR", home)
    monkeypatch.setattr(config, "DB_PATH", home / "portfolio.sqlite3")
    monkeypatch.setattr(config, "SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(config, "REPORTS_DIR", home / "reports")
    monkeypatch.setenv("PORTFOLIO_INTEGRATIONS_PATH", str(home / "integrations.enc"))
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "csrf-test-admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("csrf-test-password"))
    monkeypatch.setenv("PORTFOLIO_MCP_API_TOKEN", "csrf-test-bearer")
    db = Database(home / "portfolio.sqlite3")
    db.init_db()
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setattr(web, "MCPServer", lambda: MCPServer(lambda: db))
    client = web.app.test_client()
    assert client.post("/login", data={"username": "csrf-test-admin", "password": "csrf-test-password"}).status_code == 302
    with client.session_transaction() as session:
        token = session["csrf_token"]
    return client, token, db


def test_kill_switch_requires_token_before_mutating(isolated_client):
    client, token, db = isolated_client
    assert client.post("/api/risk/kill-switch", json={"state": "on"}).status_code == 403
    assert db.get_console_state("global_kill_switch", "off") == "off"
    assert client.post("/api/risk/kill-switch", json={"state": "on"}, headers={"X-CSRF-Token": token}).status_code == 200
    assert db.get_console_state("global_kill_switch", "off") == "on"


def test_settings_form_requires_token_and_renders_it(isolated_client):
    client, token, _ = isolated_client
    page = client.get("/page/settings")
    assert page.status_code == 200
    assert 'name="csrf_token"' in page.get_data(as_text=True)
    assert client.post("/settings", data={"owner_name": "attacker"}).status_code == 403
    assert client.get("/api/settings").get_json()["owner_name"] != "attacker"
    response = client.post("/settings", data={"owner_name": "operator", "csrf_token": token})
    assert response.status_code == 302
    assert client.get("/api/settings").get_json()["owner_name"] == "operator"


def test_api_patch_and_cookie_logout_require_token(isolated_client):
    client, token, _ = isolated_client
    assert client.patch("/api/settings", json={"owner_name": "attacker"}).status_code == 403
    assert client.get("/api/settings").get_json()["owner_name"] != "attacker"
    assert client.patch("/api/settings", json={"owner_name": "operator"}, headers={"X-CSRF-Token": token}).status_code == 200
    assert client.post("/logout").status_code == 403
    assert client.get("/api/settings").status_code == 200
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302


def test_origin_or_bad_token_cannot_replace_valid_token(isolated_client):
    client, _, db = isolated_client
    headers = {"Origin": "http://localhost"}
    assert client.post("/api/risk/kill-switch", json={"state": "on"}, headers=headers).status_code == 403
    assert client.post("/api/risk/kill-switch", json={"state": "on"}, headers={**headers, "X-CSRF-Token": "invalid"}).status_code == 403
    assert db.get_console_state("global_kill_switch", "off") == "off"


def test_all_session_mutation_routes_reject_missing_token(isolated_client):
    client, _, _ = isolated_client
    for rule in web.app.url_map.iter_rules():
        if rule.endpoint in {"login", "static"}:
            continue
        path = re.sub(r"<(?:(?:int|float|path|string):)?([^>]+)>", lambda m: "1" if m.group(1) in {"transaction_id", "finding_id", "approval_id", "goal_id", "alert_id", "run_id"} else "test", rule.rule)
        for method in sorted(rule.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            response = client.open(path, method=method, json={})
            assert response.status_code == 403, (method, rule.rule, response.status_code)


def test_mcp_bearer_mutation_needs_no_csrf_but_session_does(isolated_client):
    client, token, db = isolated_client
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "paper_proposal_create", "arguments": {"symbol": "INFY", "side": "buy", "quantity": "1"}}}
    assert client.post("/mcp", json=payload).status_code == 403
    bearer_only = web.app.test_client()
    response = bearer_only.post("/mcp", json=payload, headers={"Authorization": "Bearer csrf-test-bearer"})
    assert response.status_code == 200
    assert response.get_json()["result"]["isError"] is False
    assert any(row["event_type"] == "mcp.paper_proposal_created" for row in db.list_audit_events())
    assert client.post("/mcp", json=payload, headers={"X-CSRF-Token": token}).status_code == 200
