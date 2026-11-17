from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash

from .config import DEFAULT_SETTINGS, OFFICIAL_MCP_ENDPOINTS, load_settings, save_settings, oauth_provider_config
from .provider_mcp import MCPClient, MCPProviderAdapter, MCPAuthorizationRequired
from .agents import AGENTS, list_agents, retry_agent, run_agent
from . import integration_auth
from .db import Database
from .execution import ExecutionGateway, LiveModeRejected
from .reporting import generate_report
from .risk import RiskEngine, RiskLimits
from .strategies import MarketFact, PortfolioFact, StrategyInputError, propose_kite_short_term, propose_upstox_long_term, propose_indmoney_us_tracking
from .trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce
from .provider_data import ProviderSyncService
from .promotion_gates import PromotionGates
from .mcp_server import MCPServer, token_valid, redact
from .ledger import FinancialEvent, calculate, export_data, import_rows
from .market_data import MarketDataService, BenchmarkSnapshot, _now
from .analytics import portfolio_analytics
from .research import add_evidence, run_research, portfolio_health, concentration_risk, placeholder
from .finance_ops import (add_cash_flow, add_goal_contribution, add_liability, acknowledge_alert, backup_payload, correct_transaction, create_alert, create_goal, daily_report_data, list_alert_deliveries, list_goals, restore_backup, set_alert_delivery, write_backup)
from .brokers.base import BrokerAdapterError
from .brokers.kite import KiteAdapter, KiteConfig
from .brokers.upstox import UpstoxAdapter, UpstoxConfig

app = Flask(__name__)
app.secret_key = os.environ.get("PORTFOLIO_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PORTFOLIO_PUBLIC_HTTPS", "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
_trusted_hosts = [item.strip() for item in os.environ.get("PORTFOLIO_TRUSTED_HOSTS", "").split(",") if item.strip()]
if _trusted_hosts:
    app.config["TRUSTED_HOSTS"] = _trusted_hosts


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.endpoint not in {"health", "setup", "login"}:
        response.headers.setdefault("Cache-Control", "private, no-store, max-age=0")
        response.headers.setdefault("Vary", "Cookie")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    if os.environ.get("PORTFOLIO_PUBLIC_HTTPS", "0") == "1" and (request.is_secure or forwarded_proto == "https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _allowed_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    raw = os.environ.get("PORTFOLIO_ALLOWED_CIDRS", "").strip()
    if not raw:
        return (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))
    networks = []
    for value in raw.split(","):
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _credentials_configured() -> bool:
    return bool(os.environ.get("PORTFOLIO_ADMIN_USERNAME", "").strip() and os.environ.get("PORTFOLIO_ADMIN_PASSWORD_HASH", "").strip())


def _request_allowed() -> bool:
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return any(address in network for network in _allowed_networks())


SETUP_TEMPLATE = """<!doctype html><title>Portfolio Tracker setup</title><h1>Console locked</h1><p>Admin credentials are not configured. No portfolio data or mutations are exposed.</p><p>Set <code>PORTFOLIO_ADMIN_USERNAME</code>, <code>PORTFOLIO_ADMIN_PASSWORD_HASH</code>, and <code>PORTFOLIO_SESSION_SECRET</code> in the Unraid container environment, then restart it.</p><p>Generate a password hash without storing plaintext: <code>python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))'</code></p>"""
LOGIN_TEMPLATE = """<!doctype html><title>Portfolio Tracker login</title><h1>Operator login</h1>{% if error %}<p>{{ error }}</p>{% endif %}<form method=post><label>Username <input name=username autocomplete=username required></label><label>Password <input name=password type=password autocomplete=current-password required></label><button>Sign in</button></form>"""


@app.before_request
def operator_guard():
    if not _request_allowed():
        return jsonify({"error": "request source is outside the configured LAN allowlist"}), 403
    if request.endpoint in {"setup", "login", "health"}:
        return None
    # MCP bearer credentials authenticate independently of browser cookies.
    if request.endpoint == "mcp_endpoint" and token_valid(request):
        return None
    if request.endpoint == "mcp_endpoint" and not session.get("operator_authenticated"):
        return None  # Preserve the MCP endpoint's JSON-RPC authentication error.
    if not _credentials_configured():
        if request.path.startswith("/api/"):
            return jsonify({"error": "admin credentials are not configured; console is locked", "setup_required": True}), 503
        return redirect(url_for("setup"))
    if not session.get("operator_authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("login", next=request.path))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_ok():
        return jsonify({"error": "CSRF validation failed"}), 403
    return None


@app.get("/setup")
def setup():
    return render_template_string(SETUP_TEMPLATE), 503


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _credentials_configured():
        return render_template_string(SETUP_TEMPLATE), 503
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        try:
            valid = secrets.compare_digest(username, os.environ["PORTFOLIO_ADMIN_USERNAME"]) and check_password_hash(os.environ["PORTFOLIO_ADMIN_PASSWORD_HASH"], password)
        except (KeyError, ValueError):
            valid = False
        if valid:
            session.clear(); session["operator_authenticated"] = True; session["csrf_token"] = secrets.token_urlsafe(32); session["oauth_session_id"] = secrets.token_urlsafe(32)
            return redirect(_safe_login_next(request.args.get("next")) or url_for("index"))
        error = "Invalid credentials"
    return render_template_string(LOGIN_TEMPLATE, error=error), 401 if error else 200


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "auth_configured": _credentials_configured(), "paper_only": True})





_SETTINGS_KEYS = ("owner_name", "risk_profile", "monthly_contribution", "reminder_time", "notes", "trading_mode", "max_order_notional", "max_position_weight", "scheduler_enabled", "default_account_id", "default_portfolio_id", "default_currency")


def _money(value, currency="INR"):
    try:
        prefix = "$" if currency == "USD" else "₹" if currency == "INR" else currency + " "
        return f"{prefix}{float(value):,.2f}"
    except Exception:
        return "n/a"


def _pct(value):
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "n/a"


def _payload() -> dict[str, Any]:
    return request.get_json(silent=True) or request.form.to_dict() or {}


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_ok() -> bool:
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or (_payload().get("csrf_token") if request.is_json else None)
    expected = session.get("csrf_token")
    return bool(expected and supplied and secrets.compare_digest(str(expected), str(supplied)))


def _safe_login_next(target: str | None) -> str | None:
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ord(char) < 32 or ord(char) == 127 for char in target):
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    return target


def _integration_secret_key() -> str:
    return str(app.secret_key)


def _integration_status() -> dict[str, dict[str, Any]]:
    result = {name: integration_auth.status(_integration_secret_key(), name) for name in integration_auth.PROVIDERS}
    db = Database(); db.init_db()
    for name, status in result.items():
        record = integration_auth.get_provider(_integration_secret_key(), name)
        account_id = str(record.get("account_id") or load_settings().get("default_account_id") or "operator")
        runs = db.list_provider_sync_runs(broker_id=name, account_id=account_id, limit=1)
        reconciliations = db.list_provider_reconciliations(broker_id=name, account_id=account_id, limit=1)
        run = dict(runs[0]) if runs else None
        rec = dict(reconciliations[0]) if reconciliations else None
        status.update({"account_id": account_id, "source": (run or {}).get("provider"), "last_sync": (run or {}).get("finished_at"), "last_sync_status": (run or {}).get("status"), "last_sync_error": json.loads((run or {}).get("error_json", "{}")) if run else {}, "last_reconciliation": (rec or {}).get("created_at"), "reconciliation_status": (rec or {}).get("status")})
        # Consent is not proof that the last broker snapshot is still fresh.
        stale_run = bool(run and run["status"] in {"error", "stale"})
        if run and not stale_run:
            try:
                stamp = datetime.fromisoformat(str(run["finished_at"]).replace("Z", "+00:00"))
                stale_run = stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 86400
            except (TypeError, ValueError, OverflowError):
                stale_run = True
        status["display_status"] = "stale" if stale_run else status.get("status", "unconfigured")
        mode = load_settings().get("provider_modes", {}).get(name, "mcp_only")
        endpoint = OFFICIAL_MCP_ENDPOINTS.get(name)
        status.update({"mode": mode, "transport": "mcp" if mode == "mcp_only" else "rejected", "mcp_endpoint": endpoint["url"] if endpoint else None,
                       "mcp_authorized": bool(status.get("oauth_authorized")), "discovered_tools": status.get("discovered_tools", []),
                       "mcp_status": "connected" if status.get("mcp_verified") else ("authorized" if status.get("oauth_authorized") else ("authorization_required" if endpoint else "unconfigured"))})
    return result


def _provider_adapter(name: str):
    if name not in integration_auth.PROVIDERS:
        raise ValueError("unknown provider")
    mode = load_settings().get("provider_modes", {}).get(name, "mcp_only")
    if mode != "mcp_only":
        raise ValueError("provider REST/API transport is disabled; provider mode must be mcp_only")
    endpoint = OFFICIAL_MCP_ENDPOINTS.get(name)
    if not endpoint:
        raise ValueError(f"{name} MCP endpoint is unconfigured; no verified official endpoint is available")
    record = integration_auth.get_provider(_integration_secret_key(), name)
    account_id = str(record.get("account_id") or load_settings().get("default_account_id") or "operator")
    # The callback state is bound to the initiating browser session, but the
    # encrypted consent belongs to this authenticated single-operator app.
    # Re-login must not make a saved token unusable for read-only sync.
    session_id = str(record.get("oauth_session") or "")
    def token_provider():
        token_endpoint = record.get("token_endpoint")
        refresh = (lambda refresh_token: integration_auth._post(token_endpoint, {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": record.get("client_id", "")})) if token_endpoint else None
        return integration_auth.access_token(_integration_secret_key(), name, session_id=session_id, refresh=refresh)
    client = MCPClient(endpoint["url"], provider=name, authorization_url=oauth_provider_config(name).get("authorization_url") or endpoint["url"], token_provider=token_provider)
    return MCPProviderAdapter(provider=name, broker_id=name, account_id=account_id, client=client)


def _holding_values(payload: dict[str, Any], *, portfolio: str | None = None, symbol: str | None = None) -> dict[str, Any]:
    portfolio = str(payload.get("portfolio", portfolio or "Main")).strip() or "Main"
    symbol = str(payload.get("symbol", symbol or "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    try:
        shares = float(str(payload.get("shares")))
        avg_cost = float(str(payload.get("avg_cost")))
    except (TypeError, ValueError) as exc:
        raise ValueError("shares and avg_cost must be numbers") from exc
    if not math.isfinite(shares) or shares < 0 or not math.isfinite(avg_cost) or avg_cost < 0:
        raise ValueError("shares and avg_cost must be finite, non-negative numbers")
    return {"portfolio": portfolio, "symbol": symbol, "label": str(payload.get("label", "")).strip(), "currency": str(payload.get("currency", "INR")).strip().upper() or "INR", "shares": shares, "avg_cost": avg_cost, "notes": str(payload.get("notes", "")).strip()}


def _row_json(row):
    return dict(row) if row is not None else None


@app.context_processor
def inject_helpers():
    return {"money": _money, "pct": _pct, "csrf_token": _csrf_token()}


def _broker_holdings_view(db: Database, integrations: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read-only projection; never copy MCP rows into manual or paper state."""
    groups = []
    for name in ("kite", "upstox", "indmoney"):
        status = integrations.get(name, {})
        account_id = str(status.get("account_id") or "operator")
        holdings = _json_rows(db.list_provider_holdings(broker_id=name, account_id=account_id))
        positions = _json_rows(db.list_provider_positions(broker_id=name, account_id=account_id))
        counts = {
            "holdings": len(holdings), "positions": len(positions),
            "cash": len(db.list_provider_cash(broker_id=name, account_id=account_id)),
            "orders": len(db.list_provider_orders(broker_id=name, account_id=account_id)),
            "fills": len(db.list_provider_fills(broker_id=name, account_id=account_id)),
        }
        groups.append({
            "broker_id": name, "account_id": account_id,
            "holdings": holdings, "positions": positions, "counts": counts,
            "last_sync_status": status.get("last_sync_status"), "last_sync": status.get("last_sync"),
            "display_status": status.get("display_status"),
            "reconciliation_status": status.get("reconciliation_status"),
            "read_only": True,
        })
    return groups


def _dashboard_data_status(db: Database, snapshot: Mapping[str, Any], broker_groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Read-only counts that explain which source populates each workspace."""
    with db.connect() as conn:
        def count(sql: str, params: tuple[Any, ...] = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])
        manual_events = count("SELECT COUNT(*) FROM financial_events WHERE namespace='manual'")
        broker_events = count("SELECT COUNT(*) FROM financial_events WHERE namespace='broker'")
        paper_events = count("SELECT COUNT(*) FROM financial_events WHERE namespace='paper'")
        research_runs = count("SELECT COUNT(*) FROM research_runs")
        research_evidence = count("SELECT COUNT(*) FROM research_evidence")
        research_findings = count("SELECT COUNT(*) FROM research_findings")
        goals = count("SELECT COUNT(*) FROM finance_goals")
        cash_flows = count("SELECT COUNT(*) FROM cash_flow_records")
        liabilities = count("SELECT COUNT(*) FROM liabilities")
        alerts = count("SELECT COUNT(*) FROM alerts WHERE status != 'acknowledged'")
        paper_orders = count("SELECT COUNT(*) FROM order_intents WHERE mode='paper'")
        scheduler_runs = count("SELECT COUNT(*) FROM scheduler_runs")
        audit_events = count("SELECT COUNT(*) FROM audit_events")
    totals = {key: sum(group["counts"][key] for group in broker_groups) for key in ("holdings", "positions", "cash", "orders", "fills")}
    portfolio_count = int(snapshot.get("summary", {}).get("positions", 0))
    research_count = research_runs + research_evidence + research_findings
    finance_count = goals + cash_flows + liabilities + alerts
    ledger_count = manual_events + broker_events + paper_events
    workspaces = [
        {"key": "broker", "label": "Broker snapshots", "count": totals["holdings"], "unit": "holdings", "state": "POPULATED" if totals["holdings"] else "EMPTY", "detail": f"{totals['positions']} positions · {totals['cash']} cash rows · {totals['orders']} orders · {totals['fills']} fills; snapshot rows do not create trades or ledger entries.", "href": "/#broker-holdings"},
        {"key": "manual_portfolio", "label": "Manual portfolio", "count": portfolio_count, "unit": "holdings", "state": "POPULATED" if portfolio_count else "EMPTY", "detail": "Manual holdings only; broker snapshots stay separate." if not portfolio_count else "Manual holdings and verified marks only.", "href": "/#manual-holdings"},
        {"key": "paper_trades", "label": "Paper trades", "count": paper_orders, "unit": "orders", "state": "POPULATED" if paper_orders else "EMPTY", "detail": "Paper gateway orders; provider orders are reported separately." if not paper_orders else "Paper orders only; no live execution.", "href": "/page/trades"},
        {"key": "ledger", "label": "Transaction ledger", "count": ledger_count, "unit": "events", "state": "POPULATED" if ledger_count else "EMPTY", "detail": "Manual, broker-fill, and paper events; holdings are not transactions.", "href": "/page/ledger"},
        {"key": "research", "label": "Research / stock analysis", "count": research_count, "unit": "records", "state": "POPULATED" if research_count else "EMPTY", "detail": f"{research_runs} runs · {research_evidence} evidence items · {research_findings} findings; no evidence means analysis is unavailable.", "href": "/page/stock-analysis"},
        {"key": "finance", "label": "Finance goals & records", "count": finance_count, "unit": "records", "state": "POPULATED" if finance_count else "EMPTY", "detail": f"{goals} goals · {cash_flows} cash-flow rows · {liabilities} liabilities · {alerts} active alerts.", "href": "/page/finance"},
        {"key": "scheduler", "label": "Scheduler", "count": scheduler_runs, "unit": "runs", "state": "POPULATED" if scheduler_runs else "EMPTY", "detail": "Paper/proposal-only scheduler runs; it does not submit broker orders.", "href": "/page/scheduler"},
        {"key": "audit", "label": "Audit", "count": audit_events, "unit": "events", "state": "POPULATED" if audit_events else "EMPTY", "detail": "Durable local audit events.", "href": "/page/audit"},
    ]
    return {"broker_totals": totals, "brokers": broker_groups, "workspaces": workspaces,
            "goals": daily_report_data(db)["goals"]}


def _console_snapshot() -> dict[str, Any]:
    db = Database(); db.init_db()
    snapshot, _, _ = generate_report(db, save=False)
    risk = api_risk().get_json()
    integrations = _integration_status()
    broker_holdings = _broker_holdings_view(db, integrations)
    data_status = _dashboard_data_status(db, snapshot, broker_holdings)
    return {
        "snapshot": snapshot,
        "generated_at": snapshot["generated_at"],
        "agents": list_agents(db),
        "integrations": integrations,
        "broker_holdings": broker_holdings,
        "data_status": data_status,
        "audit_events": _json_rows(db.list_audit_events(limit=25)),
        "risk": risk,
    }


@app.get("/")
def index():
    data = _console_snapshot()
    snapshot = data["snapshot"]
    portfolios = snapshot["portfolios"]
    currencies = {section["currency"] for section in portfolios}
    complete_marks = bool(portfolios) and len(currencies) == 1 and all(
        section["summary"].get("total_value") is not None and section["summary"].get("gain") is not None
        for section in portfolios
    )
    total_value = sum(Decimal(str(item["summary"]["total_value"])) for item in portfolios) if complete_marks else None
    total_gain = sum(Decimal(str(item["summary"]["gain"])) for item in portfolios) if complete_marks else None
    return render_template_string(
        OPERATOR_PAGE_TEMPLATE,
        section="portfolio", description="Local holdings, market status, risk posture, and paper-only operator actions.", data={},
        summary=snapshot["summary"],
        portfolios=portfolios,
        total_value=total_value,
        total_gain=total_gain,
        currency=portfolios[0]["currency"] if portfolios else "INR",
        snapshot_time=snapshot["generated_at"][:19].replace("T", " ") + " UTC",
        agents=data["agents"], integrations=data["integrations"], audit_events=data["audit_events"], risk=data["risk"],
        broker_holdings=data["broker_holdings"],
        data_status=data["data_status"],
    )


@app.get("/api/console/snapshot")
def api_console_snapshot():
    data = _console_snapshot()
    snapshot = data["snapshot"]
    return jsonify({"generated_at": data["generated_at"], "summary": snapshot["summary"], "broker_holdings": data["broker_holdings"], "data_status": data["data_status"], "risk": data["risk"], "agents": data["agents"], "integrations": data["integrations"], "audit_events": data["audit_events"]})


@app.post("/settings")
def update_settings():
    settings = {**DEFAULT_SETTINGS, **load_settings()}
    for key in _SETTINGS_KEYS:
        if key in request.form:
            settings[key] = request.form.get(key, settings.get(key, ""))
    if settings.get("risk_profile") not in {"low", "balanced", "aggressive"}:
        return jsonify({"error": "risk_profile must be low, balanced, or aggressive"}), 400
    if settings.get("trading_mode") not in {"paper", "approval_required"}:
        return jsonify({"error": "trading_mode must be paper or approval_required"}), 400
    try:
        max_order_text = str(settings.get("max_order_notional", "")).strip()
        max_weight_text = str(settings.get("max_position_weight", "")).strip()
        if max_order_text and Decimal(max_order_text) <= 0:
            raise ValueError
        if max_weight_text and not (Decimal("0") < Decimal(max_weight_text) <= Decimal("1")):
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        return jsonify({"error": "max order notional must be positive and max position weight must be between 0 and 1"}), 400
    settings["scheduler_enabled"] = str(settings.get("scheduler_enabled", "false")).lower() in {"true", "on", "1", "yes"}
    save_settings(settings)
    return redirect(url_for("operator_page", section="settings"))


@app.post("/holdings")
def add_holding():
    db = Database(); db.init_db()
    db.upsert_holding(symbol=request.form["symbol"].strip().upper(), label=request.form.get("label", "").strip(), shares=float(request.form["shares"]), avg_cost=float(request.form["avg_cost"]), notes=request.form.get("notes", "").strip(), portfolio=request.form.get("portfolio", "Main").strip() or "Main", currency=request.form.get("currency", "INR").strip().upper() or "INR")
    return redirect(url_for("index"))


@app.post("/transactions")
def add_transaction():
    db = Database(); db.init_db()
    db.adjust_holding(symbol=request.form["symbol"].strip().upper(), delta_shares=float(request.form["shares"]), trade_price=float(request.form["price"]), side=request.form["side"], fees=float(request.form.get("fees", 0) or 0), trade_date=request.form.get("trade_date") or None, notes=request.form.get("notes", "").strip(), portfolio=request.form.get("portfolio", "Main").strip() or "Main", currency=request.form.get("currency", "INR").strip().upper() or "INR")
    return redirect(url_for("index"))


@app.get("/api/settings")
def api_get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["PUT", "PATCH"])
def api_update_settings():
    payload = _payload()
    settings = {**DEFAULT_SETTINGS, **load_settings()}
    for key in _SETTINGS_KEYS:
        if key in payload:
            settings[key] = payload[key]
    if settings.get("risk_profile") not in {"low", "balanced", "aggressive"}:
        return jsonify({"error": "risk_profile must be low, balanced, or aggressive"}), 400
    if settings.get("trading_mode") not in {"paper", "approval_required"}:
        return jsonify({"error": "trading_mode must be paper or approval_required"}), 400
    try:
        max_order_text = str(settings.get("max_order_notional", "")).strip()
        max_weight_text = str(settings.get("max_position_weight", "")).strip()
        if max_order_text and Decimal(max_order_text) <= 0:
            raise ValueError
        if max_weight_text and not (Decimal("0") < Decimal(max_weight_text) <= Decimal("1")):
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        return jsonify({"error": "max order notional must be positive and max position weight must be between 0 and 1"}), 400
    settings["scheduler_enabled"] = bool(settings.get("scheduler_enabled")) if isinstance(settings.get("scheduler_enabled"), bool) else str(settings.get("scheduler_enabled", "false")).lower() in {"true", "on", "1", "yes"}
    save_settings(settings)
    return jsonify(settings)


@app.get("/api/holdings")
def api_list_holdings():
    db = Database(); db.init_db()
    return jsonify({"holdings": [_row_json(row) for row in db.list_holdings()]})


@app.post("/api/holdings")
def api_create_holding():
    try:
        values = _holding_values(_payload())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db = Database(); db.init_db(); db.upsert_holding(**values)
    return jsonify(_row_json(db.get_holding(portfolio=values["portfolio"], symbol=values["symbol"]))), 201


@app.put("/api/holdings/<portfolio>/<symbol>")
def api_update_holding(portfolio: str, symbol: str):
    try:
        values = _holding_values({**_payload(), "portfolio": portfolio, "symbol": symbol}, portfolio=portfolio, symbol=symbol)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db = Database(); db.init_db()
    if db.get_holding(portfolio=portfolio, symbol=symbol) is None:
        return jsonify({"error": "holding not found"}), 404
    db.upsert_holding(**values)
    return jsonify(_row_json(db.get_holding(portfolio=portfolio, symbol=symbol)))


@app.delete("/api/holdings/<portfolio>/<symbol>")
def api_delete_holding(portfolio: str, symbol: str):
    db = Database(); db.init_db()
    try:
        deleted = db.delete_holding(portfolio=portfolio, symbol=symbol)
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "non_zero_shares"}), 409
    if not deleted:
        return jsonify({"error": "holding not found"}), 404
    return jsonify({"status": "deleted", "portfolio": portfolio, "symbol": symbol.upper()})


@app.get("/refresh")
def refresh():
    snapshot, markdown, path = generate_report(Database(), save=False)
    return jsonify({"status": "ok", "report_path": path, "summary": snapshot["summary"], "markdown": markdown})


@app.get("/api/report")
def api_report():
    db = Database(); db.init_db()
    snapshot, markdown, path = generate_report(db, save=False)
    return jsonify({"snapshot": snapshot, "broker_holdings": _broker_holdings_view(db, _integration_status()), "report_path": path, "markdown": markdown})


def _json_rows(rows):
    result = []
    for row in rows:
        item = dict(row)
        for key, value in list(item.items()):
            if isinstance(value, bytes):
                item[key] = value.decode("utf-8", "replace")
            if key.endswith("payload") and isinstance(value, str):
                try: item[key] = json.loads(value)
                except json.JSONDecodeError: pass
        result.append(item)
    return result


def _decimal_or_none(value):
    if value in (None, "", False):
        return None
    return Decimal(str(value))


OPERATOR_PAGE_TEMPLATE = r'''
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ section|upper }} // OM TERMINAL</title>
<style>:root{color-scheme:dark;--bg:#081018;--surface:#0d1720;--surface-raised:#111f2a;--surface-soft:#0a141c;--line:#203443;--line-strong:#315063;--text:#e7eef2;--muted:#8aa0ad;--muted-strong:#b3c3ca;--amber:#f2b84b;--cyan:#62d6ea;--green:#55d39a;--red:#ff7180;--shadow:0 18px 48px #0008;--radius:8px;--sans:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;background:radial-gradient(circle at 70% -10%,#15303d 0,transparent 35%),var(--bg);color:var(--text);font:13px/1.5 var(--sans)}a{color:var(--cyan);text-decoration:none}button,input,select,textarea{font:inherit}button,.button{border:1px solid var(--line-strong);border-radius:6px;background:linear-gradient(180deg,#17303c,#10242e);color:var(--text);padding:8px 12px;cursor:pointer;transition:.16s ease}button:hover,.button:hover{border-color:var(--cyan);background:#1a3b48;transform:translateY(-1px)}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}.terminal{min-height:100vh;display:block;padding-top:64px}.topbar{display:flex;align-items:center;gap:16px;padding:0 22px;background:#0c161fdd;backdrop-filter:blur(14px);border-bottom:1px solid var(--line);position:fixed;inset:0 0 auto;z-index:5;box-shadow:0 6px 22px #0004}.brand{font-family:var(--mono);font-size:15px;font-weight:800;color:var(--amber);letter-spacing:.1em;white-space:nowrap}.command{flex:0 1 480px;display:flex;align-items:center;gap:8px;border:1px solid var(--line-strong);border-radius:7px;background:#071018;padding:6px 9px}.command input{width:100%;border:0;outline:0;background:transparent;color:var(--text)}.command button{padding:5px 9px;font-size:11px}.tickers{margin-left:auto;display:flex;gap:14px;white-space:nowrap;font-family:var(--mono);font-size:11px;color:var(--muted)}.up{color:var(--green)}.warn{color:var(--amber)}.down{color:var(--red)}.muted{color:var(--muted)}.layout{display:grid;grid-template-columns:224px minmax(0,1fr);gap:1px;background:var(--line)}.nav{padding:18px 12px;background:#0a141c;min-height:calc(100vh - 94px);border-right:1px solid var(--line)}.nav h2{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.14em;margin:10px 10px 7px}.nav a,.nav button{display:block;width:100%;text-align:left;padding:10px 12px;border:0;border-left:3px solid transparent;border-radius:6px;background:transparent;color:var(--muted-strong);font-size:13px}.nav a:hover,.nav a.active,.nav button:hover{background:#142a35;border-left-color:var(--amber);color:#fff}.nav a.active{box-shadow:inset 0 0 0 1px #264654}.nav .safe{margin:18px 2px 0;padding:11px;border:1px solid #28634f;border-radius:6px;background:#0a201b;color:var(--green);font-family:var(--mono);font-size:10px;line-height:1.6}.main{padding:22px;min-width:0;background:transparent}.dashboard{display:grid;grid-template-columns:minmax(0,1fr) 320px;grid-template-rows:auto 1fr;gap:14px;background:transparent}.panel{border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,#0f1c26,#0b151d);min-width:0;overflow:hidden;box-shadow:0 8px 25px #0002}.panel-title{padding:12px 15px;background:#122532;border-bottom:1px solid var(--line);color:var(--amber);font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.08em}.watch{grid-column:1;grid-row:1}.watchgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line)}.quote{background:var(--surface-soft);padding:16px;min-height:132px}.symbol{font-family:var(--mono);color:var(--cyan);font-weight:700;letter-spacing:.04em}.price{font-family:var(--mono);font-size:23px;font-weight:700;margin:12px 0 6px;color:#f4f7f8}.blotter{grid-column:1;grid-row:2}.coverage{grid-column:1;grid-row:3}.coverage-body{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(220px,1fr);gap:12px;padding:12px 15px}.coverage-brokers{display:flex;gap:8px;flex-wrap:wrap;padding:12px 15px 0}.coverage-broker{flex:1 1 190px;border:1px solid var(--line);border-radius:6px;padding:9px 11px;background:#0a141c}.coverage-broker b{font:700 11px var(--mono);color:var(--cyan)}.coverage-broker small{display:block;color:var(--muted);margin-top:3px}.coverage-table{min-width:560px}.coverage-table td:last-child{white-space:normal}.goal-list{display:grid;gap:8px}.goal-row{padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#0a141c}.goal-row progress{width:100%;height:8px;accent-color:var(--green)}.broker{grid-column:1/-1;grid-row:4}.broker-group{padding:10px 15px;border-bottom:1px solid var(--line)}.broker-group h3{margin:0 0 7px;font:700 12px var(--mono);color:var(--cyan)}.broker-group .table-wrap{margin-top:8px}.right{grid-column:2;grid-row:1/4}.metric{display:flex;justify-content:space-between;gap:12px;padding:12px 15px;border-bottom:1px solid #1a2c37}.metric b{font-family:var(--mono);color:var(--cyan)}.agent,.alert{padding:12px 15px;border-bottom:1px solid #1a2c37}.badge{float:right;color:var(--green);font:11px var(--mono)}.empty{padding:30px;text-align:center;color:var(--muted)}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:11px 13px;border-bottom:1px solid #1a2c37;white-space:nowrap}th{color:var(--muted);font:10px var(--mono);letter-spacing:.1em}td:nth-child(n+3){font-family:var(--mono);font-size:12px}tbody tr:hover{background:#132833}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.span-6{grid-column:span 6}.span-12{grid-column:span 12}.card{border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,#0f1c26,#0b151d);padding:18px;min-width:0;box-shadow:0 8px 25px #0002}.card h1,.card h2{margin:0 0 8px}.card h1{font-size:24px;letter-spacing:-.02em}.card h2{font-size:14px;color:var(--amber);font-family:var(--mono);letter-spacing:.04em}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}label{display:grid;gap:6px;color:var(--muted);font-size:12px}input,select,textarea{width:100%;border:1px solid var(--line-strong);border-radius:6px;background:#08131c;color:var(--text);padding:9px 10px}textarea{min-height:100px;resize:vertical}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.danger{color:#ffb4bd!important;border-color:#713342!important;background:#321a25!important}.status{margin-top:13px;padding:11px 13px;border-left:3px solid var(--cyan);border-radius:4px;background:#0a2430;white-space:pre-wrap;min-height:37px;font-family:var(--mono);font-size:12px}.status.error{border-color:var(--red);background:#2a151c}.stale{color:var(--amber);font-family:var(--mono);font-size:12px}.footer{padding:6px 22px;background:#0c161f;border-top:1px solid var(--line);color:var(--muted);font:11px var(--mono);pointer-events:none;position:relative;z-index:1}.toast{position:fixed;right:20px;bottom:46px;max-width:420px;padding:12px 14px;border-radius:7px;background:#102a34;border:1px solid var(--cyan);box-shadow:var(--shadow);visibility:hidden;opacity:0;pointer-events:none;z-index:10;font-family:var(--mono);font-size:12px;transition:opacity .16s ease}.toast.show{visibility:visible;opacity:1}.palette{position:fixed;inset:0;background:#0009;visibility:hidden;opacity:0;pointer-events:none;place-items:start center;padding-top:13vh;z-index:20;transition:opacity .12s ease}.palette.open{visibility:visible;opacity:1;pointer-events:auto;display:grid}.palette-box{width:min(640px,92vw);background:var(--surface-raised);border:1px solid var(--amber);border-radius:10px;padding:10px;box-shadow:var(--shadow)}.palette-box input{margin-bottom:8px}.commands{display:grid;gap:3px}.commands button{text-align:left;border:0;background:transparent}.commands button small{display:block;color:var(--muted);margin-top:2px}@media(max-width:1120px){.layout{grid-template-columns:196px minmax(0,1fr)}.dashboard{grid-template-columns:minmax(0,1fr) 280px}.watchgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:820px){.terminal{display:block;padding-top:126px}.topbar{position:fixed;flex-wrap:wrap;height:auto;min-height:64px;padding:10px 14px}.command{order:3;flex-basis:100%;max-width:none}.tickers{display:none}.layout{display:grid;grid-template-columns:176px minmax(0,1fr)}.nav{min-height:calc(100vh - 94px);display:block;overflow:visible;padding:12px 8px;border-right:1px solid var(--line);border-bottom:0}.nav h2,.nav .safe{display:block}.nav a,.nav button{white-space:normal;width:100%;font-size:11px;padding:9px 8px}.main{padding:14px}.dashboard{display:flex;flex-direction:column}.coverage-body{grid-template-columns:1fr}.right{order:3}.grid{display:block}.card{margin-bottom:14px}.span-6,.span-12{display:block}.form-grid{grid-template-columns:1fr}.footer{position:relative}}@media(max-width:480px){.terminal{grid-template-columns:1fr}.layout{grid-template-columns:150px minmax(0,1fr)}.nav{padding:10px 5px}.nav a,.nav button{font-size:10px;padding:8px 5px}.nav h2{font-size:9px;margin-left:5px}.watchgrid{grid-template-columns:1fr}.main{padding:10px}.card{padding:14px}.panel-title{padding:11px 12px}.topbar{padding:9px 10px}.brand{font-size:13px}}</style></head><body><div class="terminal"><header class="topbar"><div class="brand">OM//TERMINAL</div><form class="command" id="command-form"><span class="muted">&gt;</span><input id="command-input" aria-label="Command" placeholder="portfolio, risk, agents..." autocomplete="off"><button>RUN</button></form><button id="palette-button" type="button">⌘K COMMANDS</button><div class="tickers">DATA <b class="warn">LOCAL</b> · MODE <b class="up">PAPER</b></div></header><main class="layout"><nav class="nav" aria-label="Terminal navigation"><h2>WORKSPACES</h2>{% for href,label in [('/','Portfolio'),('/page/stock-analysis','Stock analysis'),('/page/trades','Trades'),('/page/ledger','Ledger / Tax'),('/page/agents','Agents'),('/page/integrations','Integrations'),('/page/strategies','Strategies'),('/page/scheduler','Scheduler'),('/page/audit','Risk / Audit'),('/page/settings','Settings')] %}<a href="{{ href }}" class="{{ 'active' if request.path == href else '' }}"{% if request.path == href %} aria-current="page"{% endif %}>{{ label }}</a>{% endfor %}<h2>QUICK ACTIONS</h2><a href="/page/audit">Run risk review</a><button type="button" data-action="kill-switch">Enable kill switch</button><div class="safe">● AUTHENTICATED<br><span class="muted">PAPER ONLY · LIVE WRITES OFF</span></div></nav><section class="main">{% if section == 'portfolio' %}<div class="dashboard">
<section class="panel watch"><div class="panel-title">MARKET / WATCHLIST // HONEST DATA STATUS</div><div class="watchgrid"><div class="quote"><span class="symbol">MANUAL VALUE</span><div class="price {{ 'warn' if total_value is none else '' }}">{{ money(total_value, currency) if total_value is not none else 'UNAVAILABLE' }}</div><span class="muted">manual holdings only; complete verified marks required</span></div><div class="quote"><span class="symbol">MANUAL P/L</span><div class="price {{ 'warn' if total_gain is none else ('up' if total_gain >= 0 else 'down') }}">{{ money(total_gain, currency) if total_gain is not none else 'UNAVAILABLE' }}</div><span class="muted">manual holdings only; no cross-currency sum</span></div><div class="quote"><span class="symbol">MARKET DATA</span><div class="price warn">UNAVAILABLE</div><span class="muted">No live quote feed configured; marks may be n/a.</span></div><div class="quote"><span class="symbol">LAST REFRESH</span><div class="price" id="last-refresh">{{ snapshot_time }}</div><span class="muted" id="refresh-state">authenticated polling · 30s</span></div></div></section>
<section class="panel blotter" id="manual-holdings"><div class="panel-title">PORTFOLIO / HOLDINGS BLOTTER <span class="muted">· {{ summary.positions }} positions</span></div><div class="table-wrap"><table><thead><tr><th>PORTFOLIO</th><th>SYMBOL</th><th>SHARES</th><th>AVG COST</th><th>MARK</th><th>VALUE</th><th>WEIGHT</th><th>STATE</th></tr></thead><tbody>{% for section in portfolios %}{% for h in section.holdings %}<tr><td>{{ section.portfolio }}</td><td class="symbol">{{ h.symbol }}</td><td>{{ '%.4f'|format(h.shares) }}</td><td>{{ money(h.avg_cost, h.currency) }}</td><td>{{ money(h.current_price, h.currency) if h.current_price is not none else 'n/a' }}<span class="muted"> {{ h.source }}</span></td><td>{{ money(h.market_value, h.currency) }}</td><td>{{ pct(h.weight * 100) if h.weight is not none else 'n/a' }}</td><td class="{{ 'up' if h.current_price is not none else 'warn' }}">{{ 'MARKED' if h.current_price is not none else 'STALE' }}</td></tr>{% endfor %}{% else %}<tr><td colspan="8" class="empty">No local holdings. Use Trades or the existing portfolio forms to add paper data.</td></tr>{% endfor %}</tbody></table></div><div class="panel-title">RECENT LEDGER / AUDIT EVENTS</div><div class="table-wrap"><table><thead><tr><th>TIME</th><th>EVENT</th><th>ENTITY</th><th>MODE</th></tr></thead><tbody>{% for e in audit_events %}<tr><td>{{ e.created_at }}</td><td>{{ e.event_type }}</td><td>{{ e.entity_type }} {{ e.entity_id }}</td><td>{{ e.mode }}</td></tr>{% else %}<tr><td colspan="4" class="empty">No audit events recorded.</td></tr>{% endfor %}</tbody></table></div></section>
<section class="panel coverage" id="data-coverage"><div class="panel-title">DATA COVERAGE // CURRENT SOURCE STATUS <span class="muted">· empty means no records in that source, not a failed broker sync</span></div><div class="coverage-brokers">{% for group in data_status.brokers %}<div class="coverage-broker"><b>{{ group.broker_id|upper }} · {{ group.account_id }}</b><small>{{ group.counts.holdings }} holdings · {{ group.counts.positions }} positions · {{ group.counts.cash }} cash · {{ group.counts.orders }} orders · {{ group.counts.fills }} fills</small><small>{{ group.display_status|upper }} · sync {{ group.last_sync_status or 'never' }} · reconciliation {{ group.reconciliation_status or 'unavailable' }}</small></div>{% endfor %}</div>{% if not data_status.broker_totals.orders and not data_status.broker_totals.fills %}<p class="muted" style="padding:0 15px">No provider orders or fills are stored in these snapshots. Broker holdings are read-only snapshots and are not paper trades or ledger entries.</p>{% endif %}<div class="coverage-body"><div class="table-wrap"><table class="coverage-table"><thead><tr><th>WORKSPACE / SOURCE</th><th>COUNT</th><th>STATE</th><th>WHY / SCOPE</th><th>OPEN</th></tr></thead><tbody>{% for item in data_status.workspaces %}<tr><td>{{ item.label }}</td><td>{{ item.count }} {{ item.unit }}</td><td class="{{ 'up' if item.state == 'POPULATED' else 'warn' }}">{{ item.state }}</td><td>{{ item.detail }}</td><td><a href="{{ item.href }}">View</a></td></tr>{% endfor %}</tbody></table></div><div class="goal-list"><h3>FINANCIAL GOALS <a href="/page/finance">Manage</a></h3>{% for goal in data_status.goals[:5] %}<div class="goal-row"><b>{{ goal.name }}</b><span class="muted"> · {{ goal.progress.percent }}% · {{ goal.currency }}</span><progress value="{{ goal.progress.percent }}" max="100"></progress><small>{{ goal.progress.current }} / {{ goal.progress.target }} {{ goal.currency }}</small></div>{% else %}<div class="empty">No goals saved yet. <a href="/page/finance">Create a goal</a>; it will appear here.</div>{% endfor %}</div></div></section>
<section class="panel broker" id="broker-holdings"><div class="panel-title">BROKER MCP HOLDINGS <span class="muted">· read-only snapshots · native currencies</span></div><p class="muted" style="padding:0 15px">Marks unavailable from MCP holdings alone. No live price, cross-currency total, or paper position is inferred from average cost. Broker data is separate from the manual valuation above.</p>
{% for group in broker_holdings %}<div class="broker-group" id="broker-holdings-{{ group.broker_id }}"><h3>{{ group.broker_id|upper }} · {{ group.holdings|length }} holdings · {{ group.positions|length }} open positions</h3><span class="muted">Local account scope: {{ group.account_id }} · Last sync: {{ group.last_sync or 'never' }} ({{ group.last_sync_status or 'never' }}) · Reconciliation: {{ group.reconciliation_status or 'unavailable' }}{% if group.display_status == 'stale' %} · cached snapshot; latest read failed or is older than 24h{% endif %}</span><div class="table-wrap"><table><thead><tr><th>TYPE</th><th>SYMBOL</th><th>QUANTITY</th><th>AVG COST</th><th>CURRENCY</th><th>PROVIDER REFERENCE</th><th>SOURCE</th><th>CAPTURED AT</th><th>SYNCED AT</th></tr></thead><tbody>{% for h in group.holdings %}<tr data-broker="{{ group.broker_id }}"><td>HOLDING</td><td class="symbol">{{ h.symbol }}</td><td>{{ h.quantity }}</td><td>{{ h.average_price if h.average_price is not none else 'n/a' }}</td><td>{{ h.currency }}</td><td>{{ h.provider_id or '—' }}</td><td>{{ h.source }}</td><td>{{ h.captured_at }}</td><td>{{ h.synced_at }}</td></tr>{% endfor %}{% for h in group.positions %}<tr data-broker="{{ group.broker_id }}"><td>POSITION</td><td class="symbol">{{ h.symbol }}</td><td>{{ h.quantity }}</td><td>{{ h.average_price if h.average_price is not none else 'n/a' }}</td><td>{{ h.currency }}</td><td>{{ h.provider_id or '—' }}</td><td>{{ h.source }}</td><td>{{ h.captured_at }}</td><td>{{ h.synced_at }}</td></tr>{% endfor %}{% if not group.holdings and not group.positions %}<tr><td colspan="9" class="empty">No verified broker rows in this local account scope.</td></tr>{% endif %}</tbody></table></div></div>{% endfor %}</section>
<aside class="panel right"><div class="panel-title">RISK MONITOR</div><div class="metric"><span>Kill switch</span><b class="{{ 'down' if risk.global_kill_switch else 'up' }}">{{ 'ON' if risk.global_kill_switch else 'OFF' }}</b></div><div class="metric"><span>Trading mode</span><b>{{ risk.trading_mode|upper }}</b></div><div class="metric"><span>Max order</span><b>{{ risk.max_order_notional or 'unset' }}</b></div><div class="metric"><span>Max position</span><b>{{ risk.max_position_weight or 'unset' }}</b></div><div class="panel-title">AGENT MONITOR</div>{% for agent in agents %}<div class="agent"><b>{{ agent.name }}</b><span class="badge">{{ agent.last_run_status or 'READY'|upper }}</span><br><span class="muted">{{ agent.mode }} · {{ agent.last_run_finished_at or 'never run' }}</span></div>{% else %}<div class="empty">No agents registered.</div>{% endfor %}<div class="panel-title">INTEGRATIONS</div>{% for name,status in integrations.items() %}<div class="metric"><span>{{ name|upper }}</span><b class="{{ 'up' if status.display_status == 'connected' else 'warn' }}">{{ status.display_status|upper }}</b></div>{% endfor %}<div class="panel-title">ALERTS / POSTURE</div><div class="alert"><span class="up">●</span> {{ 'Kill switch engaged; paper orders blocked.' if risk.global_kill_switch else 'Paper-only safety boundary active.' }}</div><div class="alert"><span class="warn">●</span> Live market connectivity is not claimed.</div></aside></div>{% else %}<div class="panel-title">{{ section|upper }} // WORKSPACE <span class="muted">· authenticated polling enabled</span></div><div class="card"><h1>{{ section|title }}</h1><p class="muted">{{ description }}</p><p class="stale">STATUS: LOCAL DATA · provider connectivity and marks are shown only when configured.</p></div>
{% if section == 'stock-analysis' %}<div class="grid"><div class="card span-12"><h2>STOCK ANALYSIS / EVIDENCE-GATED</h2><p class="muted">Analyze any ticker, whether or not it is already held. Research uses the existing local <code>market_research</code> agent and only matching verified evidence; it never fetches data or creates a recommendation.</p><form id="stock-analysis-form" class="form-grid" method="get" action="/page/stock-analysis"><label>Ticker symbol<input name="symbol" value="{{ data.symbol }}" placeholder="INFY or AAPL" maxlength="20" pattern="[A-Za-z0-9][A-Za-z0-9.-]{0,19}" required></label><div class="actions"><button type="submit">Open ticker</button><button type="button" id="run-stock-analysis" {% if not data.symbol %}disabled{% endif %}>Run market_research</button></div></form><p class="stale">Quotes: UNAVAILABLE · No live quote, news, or fundamentals provider is configured; those fields remain unavailable. This page does not provide investment recommendations.</p><div id="stock-analysis-result" class="status" role="status">{% if data.research_run %}HISTORICAL / LAST RUN · {{ data.research_run.status|upper }} · {{ data.research_run.result.message or 'Research run stored.' }}{% else %}UNAVAILABLE · No current verified external evidence has been supplied; no external call was made and no claim was fabricated.{% endif %}</div></div>
<div class="card span-6"><h2>MANUAL HOLDINGS SNAPSHOT</h2><p class="muted">Source: local manual portfolio records only. Cost basis is not a market quote.</p>{% if data.manual %}<div class="table-wrap"><table><thead><tr><th>PORTFOLIO</th><th>SYMBOL</th><th>SHARES</th><th>AVG COST</th><th>CURRENCY</th></tr></thead><tbody>{% for h in data.manual %}<tr><td>{{ h.portfolio }}</td><td class="symbol">{{ h.symbol }}</td><td>{{ h.shares }}</td><td>{{ h.avg_cost }}</td><td>{{ h.currency }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">{% if data.symbol %}No manual holdings for this ticker.{% else %}Enter a ticker to inspect manual holdings.{% endif %}</p>{% endif %}</div>
<div class="card span-6"><h2>BROKER MCP SNAPSHOTS / READ ONLY</h2><p class="muted">Provider holdings and positions stay separate from manual and paper data. Snapshot quantities and average costs are not live valuations.</p>{% if data.broker_snapshots %}{% for group in data.broker_snapshots %}<h3>{{ group.broker_id|upper }} · account {{ group.account_id }}</h3><div class="table-wrap"><table><thead><tr><th>TYPE</th><th>SYMBOL</th><th>QUANTITY</th><th>AVG COST</th><th>CURRENCY</th><th>SOURCE</th><th>CAPTURED</th></tr></thead><tbody>{% for h in group.holdings %}<tr><td>HOLDING</td><td class="symbol">{{ h.symbol }}</td><td>{{ h.quantity }}</td><td>{{ h.average_price if h.average_price is not none else 'n/a' }}</td><td>{{ h.currency }}</td><td>{{ h.source }}</td><td>{{ h.captured_at }}</td></tr>{% endfor %}{% for h in group.positions %}<tr><td>POSITION</td><td class="symbol">{{ h.symbol }}</td><td>{{ h.quantity }}</td><td>{{ h.average_price if h.average_price is not none else 'n/a' }}</td><td>{{ h.currency }}</td><td>{{ h.source }}</td><td>{{ h.captured_at }}</td></tr>{% endfor %}</tbody></table></div>{% endfor %}{% else %}<p class="empty">{% if data.symbol %}No broker snapshot rows for this ticker.{% else %}Enter a ticker to inspect broker snapshots.{% endif %}</p>{% endif %}</div>
<div class="card span-12"><h2>VERIFIED EVIDENCE / SOURCE TRAIL</h2><p class="muted">Evidence is shown with its recorded source, identifier, timestamp, and verification state. The agent rechecks freshness before producing findings.</p>{% for e in data.evidence %}<div class="panel"><div class="panel-title">{{ e.source }} · {{ e.provider }} · {{ 'VERIFIED' if e.verified else 'UNVERIFIED' }} · {{ e.captured_at }}</div><div class="card"><p>{{ e.excerpt }}</p><small class="muted">{{ e.source_identifier }} · evidence #{{ e.id }} · {{ e.topic or 'no topic' }}</small></div></div>{% else %}<p class="empty">{% if data.symbol %}No saved evidence is associated with this ticker.{% else %}Enter a ticker to inspect associated evidence.{% endif %}</p>{% endfor %}</div>
<div class="card span-12"><h2>AGENT FINDINGS · HISTORICAL / LAST RUN</h2><p class="muted">Saved findings are audit history, not a current assessment; freshness is rechecked only when you run the agent again.</p>{% if data.findings %}{% for finding in data.findings %}<div class="panel"><div class="panel-title">HISTORICAL · {{ finding.finding_type }} · {{ finding.symbol }} · {{ finding.confidence }} · {{ finding.status|upper }}</div><div class="card"><p>{{ finding.statement }}</p><p class="muted">Evidence IDs: {{ finding.evidence_ids|join(', ') }} · Risks: {{ finding.risks|join('; ') }} · Invalidation: {{ finding.invalidation|join('; ') }}</p></div></div>{% endfor %}{% else %}<p class="empty">UNAVAILABLE · No evidence-backed findings for this ticker.</p>{% endif %}</div></div>
<script>document.getElementById('run-stock-analysis')?.addEventListener('click',async()=>{const symbol=document.querySelector('#stock-analysis-form input[name="symbol"]').value;const box=document.getElementById('stock-analysis-result');box.textContent='Running local market_research agent…';try{const response=await fetch('/api/stock-analysis/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({symbol})});const result=await response.json();box.textContent=response.ok?((result.result.research_status||result.result.status||'unavailable').toUpperCase()+' · '+(result.result.message||'Agent run stored.')):(result.error||'Research request failed');if(response.ok)setTimeout(()=>location.reload(),400)}catch(error){box.textContent=String(error);box.className='status error'}});</script>
{% elif section == 'agents' %}<div class="grid"><div class="card span-12"><h2>AGENT CONTROL PLANE</h2><p class="muted">Bounded durable runs; no shell execution or live broker writes.</p>{% for agent in data.agents %}<div class="panel"><div class="panel-title">{{ agent.name }} <span class="muted">{{ agent.mode }} · {{ agent.last_run_status or 'NEVER RUN' }}</span></div><div class="card"><p>{{ agent.description }}</p><p>Enabled: <b>{{ 'YES' if agent.enabled else 'NO' }}</b> · Last run: {{ agent.last_run_finished_at or 'never' }}</p><div class="actions"><button onclick="agentAction('{{ agent.name }}','toggle',{{ 'false' if agent.enabled else 'true' }})">{{ 'Disable' if agent.enabled else 'Enable' }}</button><button onclick="agentAction('{{ agent.name }}','run',false)">Run now</button><button onclick="agentAction('{{ agent.name }}','test',true)">Dry-run</button><button onclick="agentAction('{{ agent.name }}','retry',false)">Retry</button><button onclick="agentLogs('{{ agent.name }}')">View logs</button></div></div></div>{% else %}<p class="muted">No agents registered.</p>{% endfor %}</div></div><div id="result" class="status">Ready for an agent action.</div>
{% elif section == 'analytics' %}<div class="grid"><div class="card span-12"><h2>PORTFOLIO ANALYTICS / WATCHLIST</h2><p class="muted">Verified or explicitly manual prices only. No sectors, fundamentals, or invented marks.</p><div id="analytics-state">Manual valuation: {% if data.manual_analytics.positions and data.manual_analytics.available %}{{ data.manual_analytics.value }} {{ data.manual_analytics.base_currency }}{% else %}<b class="warn">UNAVAILABLE</b> · missing/stale marks or verified FX{% endif %}. Broker MCP holdings are read-only and excluded from manual valuation.</div><div class="table-wrap"><table><thead><tr><th>READ-ONLY SOURCE</th><th>HOLDINGS</th><th>OPEN POSITIONS</th><th>SYNC</th><th>VIEW</th></tr></thead><tbody>{% for group in data.broker_holdings %}<tr><td>{{ group.broker_id|upper }}</td><td>{{ group.holdings|length }} holdings</td><td>{{ group.positions|length }}</td><td>{{ group.last_sync_status or 'never' }}</td><td><a href="/#broker-holdings-{{ group.broker_id }}">View native-currency rows</a></td></tr>{% endfor %}</tbody></table></div><p class="stale">QUOTE FRESHNESS: <a href="/api/market-data/status">inspect quote freshness</a> · BENCHMARK COMPARISON: <a href="/api/benchmarks">inspect benchmark snapshots</a></p><p class="warn">UNAVAILABLE states are shown when no configured provider or manual snapshot exists.</p></div></div>
{% elif section == 'ledger' %}<div class="grid"><div class="card span-12"><h2>LEDGER / TAX SCOPE</h2><form method="get" action="/page/ledger" class="form-grid"><label>Namespace<select name="namespace"><option value="manual" {% if data.filters.namespace == 'manual' %}selected{% endif %}>manual</option><option value="broker" {% if data.filters.namespace == 'broker' %}selected{% endif %}>broker</option><option value="indmoney" {% if data.filters.namespace == 'indmoney' %}selected{% endif %}>indmoney</option><option value="paper" {% if data.filters.namespace == 'paper' %}selected{% endif %}>paper</option></select></label><label>Portfolio<input name="portfolio" value="{{ data.filters.portfolio or '' }}" placeholder="All portfolios"></label><label>Symbol<input name="symbol" value="{{ data.filters.symbol or '' }}" placeholder="All symbols"></label><label>As of date<input name="as_of" type="date" value="{{ data.filters.as_of or '' }}"></label><div class="actions"><button>Apply filters</button><a class="button" href="{{ data.export_json }}">Export JSON</a><a class="button" href="{{ data.export_csv }}">Export CSV</a></div></form><p class="muted">Namespace isolation is explicit. Source and provenance remain attached to each imported event.</p></div><div class="card span-6"><h2>TRANSACTION IMPORT</h2><form id="ledger-import" class="form-grid"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Source<input name="source" value="operator-ui"></label><label>Namespace<select name="namespace"><option>manual</option><option>broker</option><option>indmoney</option><option>paper</option></select></label><label style="grid-column:1/-1">Rows JSON<textarea name="rows" required placeholder='[{"event_type":"buy","symbol":"INFY","quantity":"1","price":"100","event_date":"2026-01-01","source":"broker","external_id":"txn-1"}]'></textarea></label><div class="actions"><button type="button" id="ledger-preview">Preview import</button><button type="submit">Commit import</button></div></form><div id="ledger-result" class="status">Preview before commit. Commits are authenticated, CSRF-protected, append-only, and paper/local only.</div></div><div class="card span-6"><h2>FIFO / TAX SUMMARY</h2><div class="metric"><span>Realized P/L</span><b>{{ data.summary.realized_pl }}</b></div><div class="metric"><span>Unrealized P/L</span><b class="warn">UNAVAILABLE</b></div><p class="muted">Market prices are unavailable in this local ledger surface; unrealized P/L is not estimated.</p><div class="metric"><span>Income</span><b>{{ data.summary.income }}</b></div><div class="metric"><span>Fees</span><b>{{ data.summary.fees }}</b></div><div class="metric"><span>Taxes</span><b>{{ data.summary.taxes }}</b></div><p class="muted">Provenance: calculated from normalized ledger events, FIFO lots, and stored source fields.</p></div><div class="card span-12"><h2>OPEN FIFO LOTS</h2><div class="table-wrap"><table><thead><tr><th>SYMBOL</th><th>ACQUIRED</th><th>REMAINING</th><th>UNIT COST</th><th>CURRENCY</th><th>SOURCE</th></tr></thead><tbody>{% for lot in data.lots %}<tr><td class="symbol">{{ lot.symbol }}</td><td>{{ lot.date }}</td><td>{{ lot.remaining_quantity }}</td><td>{{ lot.unit_cost }}</td><td>{{ lot.currency }}</td><td>ledger event {{ lot.event_id }} · normalized source</td></tr>{% else %}<tr><td colspan="6" class="empty">No open lots in this namespace/scope.</td></tr>{% endfor %}</tbody></table></div></div><div class="card span-12"><h2>RECENT TRANSACTIONS / PROVENANCE</h2><div class="table-wrap"><table><thead><tr><th>DATE</th><th>TYPE</th><th>SYMBOL</th><th>QTY</th><th>GROSS</th><th>FEES</th><th>TAXES</th><th>SOURCE</th><th>EXTERNAL ID</th></tr></thead><tbody>{% for event in data.events %}<tr><td>{{ event.event_date }}</td><td>{{ event.event_type }}</td><td class="symbol">{{ event.symbol }}</td><td>{{ event.quantity }}</td><td>{{ event.gross_amount }} {{ event.currency }}</td><td>{{ event.fees }}</td><td>{{ event.taxes }}</td><td>{{ event.source }}</td><td>{{ event.external_id or '—' }}</td></tr>{% else %}<tr><td colspan="9" class="empty">No ledger events for this scope.</td></tr>{% endfor %}</tbody></table></div></div></div>
{% elif section == 'trades' %}<div class="grid"><div class="card span-6"><h2>PAPER ORDER TICKET</h2><form id="paper-order" class="form-grid"><label>Symbol<input name="symbol" placeholder="INFY" required></label><label>Side<select name="side"><option>buy</option><option>sell</option></select></label><label>Quantity<input name="quantity" type="number" step="0.0001" required></label><label>Quote price<input name="quote_price" type="number" step="0.01" required></label><label>Currency<input name="currency" value="INR"></label><label>Account ID<input name="account_id" value="operator"></label><label>Portfolio ID<input name="portfolio_id" value="Main"></label><label>Idempotency key<input name="idempotency_key" required></label><div class="actions"><button>Submit paper order</button><button type="button" class="danger" data-action="kill-switch">Enable kill switch</button></div></form><div id="result" class="status">Paper gateway ready. Risk checks remain active.</div></div><div class="card span-6"><h2>ORDER HISTORY</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>PAYLOAD</th><th>STATUS</th><th>MODE</th></tr></thead><tbody>{% for o in data.orders %}<tr><td>{{ o.id }}</td><td>{{ o.symbol if o.symbol is defined else o.order_payload }}</td><td>{{ o.status }}</td><td>{{ o.mode }}</td></tr>{% else %}<tr><td colspan="4" class="muted">No orders yet.</td></tr>{% endfor %}</tbody></table></div></div></div>
{% elif section == 'integrations' %}<div class="grid">{% for name,status in data.items() %}<div class="card span-6"><h2>{{ name|upper }} <span class="{{ 'up' if status.display_status == 'connected' else 'warn' }}">● {{ status.display_status|upper }}</span></h2><p class="muted">Source: {{ status.source or 'none' }} · Account: {{ status.account_id }} · Last sync: {{ status.last_sync or 'never' }} · Sync state: {{ status.last_sync_status or 'never' }} · Reconciliation: {{ status.reconciliation_status or 'never' }}{% if status.last_sync_error %} · Error: {{ status.last_sync_error.message or status.last_sync_error.code }}{% endif %}</p><p>Mode: <b>{{ status.mode|default('mcp_only') }}</b> · Transport: <b>{{ status.transport|default('mcp') }}</b> · Endpoint: <code>{{ status.mcp_endpoint or 'unconfigured' }}</code></p><p>MCP status: <b>{{ status.mcp_status|default('unconfigured')|upper }}</b> · Discovered tools: <code>{{ status.discovered_tools|join(', ') if status.discovered_tools else 'none' }}</code> · read-only; live writes disabled.</p>{% if status.mcp_endpoint %}<div class="actions"><a class="button" href="{{ url_for('integration_start', name=name) }}">Authorize / reconnect in browser</a><button type="button" onclick="syncIntegration('{{ name }}')">Discover + sync verified read</button><a class="button" href="/#broker-holdings-{{ name }}">View holdings</a><a class="button" href="{{ url_for('api_integration_snapshot', name=name) }}">Snapshot JSON</a></div>{% endif %}<p>Capabilities: read={{ 'yes' if status.capabilities.read else 'no' }}, reconciliation={{ 'yes' if status.capabilities.reconciliation else 'no' }}, trading=no. Live writes disabled.</p><div class="status">{{ 'MCP VERIFIED' if status.mcp_verified else 'MCP AUTHORIZATION / VERIFIED READ REQUIRED' }} · legacy API credentials are not used for sync.</div></div>{% endfor %}</div><div id="result" class="status">Integration actions report here.</div>
{% elif section == 'strategies' %}<div class="card"><h2>PROPOSAL TICKET</h2><form id="strategy-form" class="form-grid"><label>Strategy<select name="strategy"><option>kite-short-term/v1</option><option>upstox-long-term/v1</option><option>indmoney-us-read-only/v1</option></select></label><label>Symbol<input name="symbol" value="INFY" required></label><label>Market<input name="market" value="INDIA"></label><label>Currency<input name="currency" value="INR"></label><label>Price<input name="price" type="number" step="0.01" required></label><label>Signal<select name="signal"><option>buy</option><option>sell</option><option>hold</option></select></label><label>Quantity<input name="quantity" value="1"></label><label>Source ID<input name="source_id" value="operator-input"></label><div class="actions"><button>Generate paper proposal</button></div></form><div id="result" class="status">Proposals are suggestions only and never execute.</div></div>
{% elif section == 'scheduler' %}<div class="grid"><div class="card span-6"><h2>SCHEDULER CONTROL</h2><p>Enabled: <b>{{ 'YES' if data.enabled else 'NO' }}</b></p><div class="actions"><button onclick="postJson('/api/scheduler/state',{enabled:'true'})">Enable</button><button onclick="postJson('/api/scheduler/state',{enabled:'false'})">Disable</button><button onclick="postJson('/api/scheduler/run',{})">Run due proposals</button></div><div id="result" class="status">Scheduler is paper/proposal-only.</div></div><div class="card span-6"><h2>RECENT RUNS</h2><div class="table-wrap"><table><thead><tr><th>TIME</th><th>STATUS</th><th>DETAIL</th></tr></thead><tbody>{% for run in data.runs %}<tr><td>{{ run.created_at }}</td><td>{{ run.status }}</td><td>{{ run.message if run.message is defined else run }}</td></tr>{% else %}<tr><td colspan="3" class="muted">No scheduler runs.</td></tr>{% endfor %}</tbody></table></div></div></div>
{% elif section == 'audit' %}<div class="grid"><div class="card span-6"><h2>RISK CONTROL</h2><p>Kill switch: <b>{{ data.kill_switch|upper }}</b></p><div class="actions"><button class="danger" onclick="postJson('/api/risk/kill-switch',{state:'on'})">Turn ON</button><button onclick="postJson('/api/risk/kill-switch',{state:'off'})">Turn OFF</button></div><div id="result" class="status">Risk actions are audited and paper-only.</div></div><div class="card span-6"><h2>LIVE APPROVALS</h2><p class="muted">Approval records never execute live orders.</p>{% for a in data.approvals %}<p>{{ a.id }} · {{ a.status }} · {{ a.proposal_id }}</p>{% else %}<p class="muted">No approval records.</p>{% endfor %}</div><div class="card span-12"><h2>AUDIT EVENT STREAM</h2><div class="table-wrap"><table><thead><tr><th>TIME</th><th>EVENT</th><th>ENTITY</th><th>MODE</th></tr></thead><tbody>{% for e in data.events %}<tr><td>{{ e.created_at }}</td><td>{{ e.event_type }}</td><td>{{ e.entity_type }} {{ e.entity_id }}</td><td>{{ e.mode }}</td></tr>{% else %}<tr><td colspan="4" class="muted">No audit events.</td></tr>{% endfor %}</tbody></table></div></div></div>
{% elif section == 'finance' %}<div class="grid"><div class="card span-6"><h2>GOALS / TARGETS</h2><p class="muted">Durable progress, monthly contributions, and Decimal forecasts.</p><form id="goal-form" class="form-grid"><label>Name<input name="name" required></label><label>Target<input name="target_amount" required></label><label>Starting<input name="starting_amount" value="0"></label><label>Monthly<input name="monthly_contribution" value="0"></label><label>Annual return %<input name="annual_return" value="0"></label><button type="submit">Create goal</button></form><pre id="goals-json">{{ data.goals|tojson(indent=2) }}</pre></div><div class="card span-6"><h2>CASH FLOW / LIABILITIES</h2><p class="muted">Manual records retain namespace and provenance.</p><button onclick="postJson('/api/finance/cash-flow',{kind:'income',amount:'0',source:'operator'})">Add cash-flow record</button><button onclick="postJson('/api/finance/liabilities',{name:'New liability',balance:'0',principal:'0',source:'operator'})">Add liability</button><pre>{{ data.cash_flow|tojson(indent=2) }}</pre></div><div class="card span-6"><h2>ALERTS / DELIVERY STATE</h2><p class="muted">Risk breach, drawdown, stale quotes, sync failure, OAuth expiry, and agent failure alerts are durable and deduplicated.</p><button onclick="postJson('/api/finance/alerts',{kind:'stale_quotes',title:'Stale quotes',fingerprint:'operator-stale-quotes'})">Create stale-quote alert</button><pre>{{ data.alerts|tojson(indent=2) }}</pre></div><div class="card span-6"><h2>BACKUP / DAILY REPORT</h2><p class="muted">Exports redact secret-looking fields and are integrity checked on restore.</p><button onclick="fetch('/api/finance/backup').then(r=>r.json()).then(d=>show(JSON.stringify(d,null,2)))">Preview redacted backup</button><a class="button" href="/api/finance/daily-report">Daily report JSON</a><pre>{{ data.liabilities|tojson(indent=2) }}</pre></div></div>
{% elif section == 'settings' %}<div class="card"><h2>OPERATOR SETTINGS</h2><form method="post" action="/settings" class="form-grid"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Owner name<input name="owner_name" value="{{ data.owner_name }}"></label><label>Risk profile<select name="risk_profile">{% for option in ['low','balanced','aggressive'] %}<option value="{{ option }}" {% if data.risk_profile == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label><label>Trading mode<select name="trading_mode"><option value="paper" {% if data.trading_mode == 'paper' %}selected{% endif %}>Paper only</option><option value="approval_required" {% if data.trading_mode == 'approval_required' %}selected{% endif %}>Approval records only</option></select></label><label>Max order notional<input name="max_order_notional" value="{{ data.max_order_notional }}"></label><label>Max position weight<input name="max_position_weight" value="{{ data.max_position_weight }}"></label><label>Scheduler enabled<select name="scheduler_enabled"><option value="false" {% if not data.scheduler_enabled %}selected{% endif %}>No</option><option value="true" {% if data.scheduler_enabled %}selected{% endif %}>Yes</option></select></label><label>Default account ID<input name="default_account_id" value="{{ data.default_account_id }}"></label><label>Default portfolio ID<input name="default_portfolio_id" value="{{ data.default_portfolio_id }}"></label><label>Default currency<input name="default_currency" value="{{ data.default_currency }}"></label><label>Monthly contribution<input name="monthly_contribution" value="{{ data.monthly_contribution }}"></label><label>Reminder time<input name="reminder_time" value="{{ data.reminder_time }}"></label><label style="grid-column:1/-1">Notes<textarea name="notes">{{ data.notes }}</textarea></label><button>Save settings</button></form><p class="muted">Stored in the application data volume; not Docker environment variables.</p><div id="result" class="status">Settings validation errors are shown here after submission.</div></div>
{% endif %}{% endif %}</section></main><footer class="footer"><span class="up">● SYSTEM READY</span> · AUTHENTICATED · LOCAL DATA · PAPER MODE · <span id="footer-status">polling bounded / hidden-tab paused</span></footer></div><div id="toast" class="toast" role="status"></div><div class="palette" id="palette"><div class="palette-box"><input id="palette-input" aria-label="Command palette" placeholder="Type a command…"><div class="commands" id="commands"></div></div></div>
<script>const csrf={{ csrf_token|tojson }};const initialBrokerHoldings={% if section == 'portfolio' %}{{ broker_holdings|tojson }}{% else %}null{% endif %};const initialDashboardStatus={% if section == 'portfolio' %}{{ data_status|tojson }}{% else %}null{% endif %};const commands=[['portfolio','Open portfolio','/'],['stock-analysis','Open stock analysis','/page/stock-analysis'],['agents','Open agents','/page/agents'],['trades','Open paper trades','/page/trades'],['ledger','Open ledger and tax','/page/ledger'],['integrations','Open integrations','/page/integrations'],['strategies','Open strategies','/page/strategies'],['scheduler','Open scheduler','/page/scheduler'],['audit','Open risk and audit','/page/audit'],['settings','Open settings','/page/settings'],['finance','Open finance operations','/page/finance'],['risk-review','Run risk review','agent:risk_review'],['kill-switch','Enable kill switch','action:kill-switch']];const result=()=>document.getElementById('result');function show(msg,err=false){const b=result();if(b){b.textContent=msg;b.className='status'+(err?' error':'')}const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',3500)}async function postJson(url,payload,method='POST'){try{const r=await fetch(url,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(payload)});const d=await r.json();show(JSON.stringify(d,null,2),!r.ok);if(r.ok)setTimeout(()=>location.reload(),500)}catch(e){show(String(e),true)}}async function agentAction(name,action,value){const url=action==='toggle'?'/api/agents/'+encodeURIComponent(name):'/api/agents/'+encodeURIComponent(name)+'/'+(action==='retry'?'retry':'run');await postJson(url,action==='toggle'?{enabled:value}:{test:value},action==='toggle'?'PUT':'POST')}async function agentLogs(name){try{const r=await fetch('/api/agents/'+encodeURIComponent(name)+'/runs');show(JSON.stringify(await r.json(),null,2),!r.ok)}catch(e){show(String(e),true)}}async function syncIntegration(name){await postJson('/api/integrations/'+name+'/sync',{})}document.getElementById('paper-order')?.addEventListener('submit',async e=>{e.preventDefault();await postJson('/api/orders/paper',Object.fromEntries(new FormData(e.target)))});document.getElementById('goal-form')?.addEventListener('submit',async e=>{e.preventDefault();const p=Object.fromEntries(new FormData(e.target));await postJson('/api/finance/goals',p)})
const ledgerPayload=()=>{const f=document.getElementById('ledger-import');const p=Object.fromEntries(new FormData(f));try{p.rows=JSON.parse(p.rows)}catch(e){throw new Error('Rows JSON is invalid')}delete p.csrf_token;return p};async function ledgerImport(preview){try{const r=await fetch('/api/ledger/import'+(preview?'/preview':''),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(ledgerPayload())});const d=await r.json();const b=document.getElementById('ledger-result');if(b)b.textContent=JSON.stringify(d,null,2);if(!r.ok)throw new Error(d.error||'Ledger request failed');if(!preview)setTimeout(()=>location.reload(),500)}catch(e){const b=document.getElementById('ledger-result');if(b){b.textContent=String(e);b.className='status error'}}}document.getElementById('ledger-preview')?.addEventListener('click',()=>ledgerImport(true));document.getElementById('ledger-import')?.addEventListener('submit',e=>{e.preventDefault();ledgerImport(false)})
document.getElementById('strategy-form')?.addEventListener('submit',async e=>{e.preventDefault();const p=Object.fromEntries(new FormData(e.target));p.as_of=new Date().toISOString();p.now=p.as_of;await postJson('/api/strategies/proposals',p)})
document.querySelectorAll('[data-action="kill-switch"]').forEach(b=>b.addEventListener('click',()=>{if(confirm('Enable the global paper-trading kill switch?'))postJson('/api/risk/kill-switch',{state:'on'})}));const palette=document.getElementById('palette'),pi=document.getElementById('palette-input'),list=document.getElementById('commands');function render(q=''){list.innerHTML=commands.filter(c=>(c[0]+' '+c[1]).toLowerCase().includes(q.toLowerCase())).map(c=>`<button type="button" data-cmd="${c[0]}"><b>${c[0]}</b><small>${c[1]}</small></button>`).join('')}function openPalette(){palette.classList.add('open');render();pi.focus()}function closePalette(){palette.classList.remove('open')}document.getElementById('palette-button').onclick=openPalette;document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openPalette()}if(e.key==='Escape')closePalette()});pi.oninput=()=>render(pi.value);list.onclick=e=>{const b=e.target.closest('[data-cmd]');if(!b)return;const c=commands.find(x=>x[0]===b.dataset.cmd);closePalette();if(c[2].startsWith('/'))location.href=c[2];else if(c[2].startsWith('agent:'))agentAction(c[2].slice(6),'run',true);else if(c[2]==='action:kill-switch'&&confirm('Enable the global paper-trading kill switch?'))postJson('/api/risk/kill-switch',{state:'on'})};document.getElementById('command-form').onsubmit=e=>{e.preventDefault();const q=document.getElementById('command-input').value.trim().toLowerCase();const c=commands.find(x=>x[0]===q||x[1].toLowerCase().includes(q));if(c){if(c[2].startsWith('/'))location.href=c[2];else if(c[2].startsWith('agent:'))agentAction(c[2].slice(6),'run',true);else if(confirm('Enable the global paper-trading kill switch?'))postJson('/api/risk/kill-switch',{state:'on'})}else show('Unknown command: '+q,true)};async function poll(){if(document.hidden)return;try{const r=await fetch('/api/console/snapshot',{headers:{'X-CSRF-Token':csrf}});if(!r.ok)throw new Error('Polling failed: HTTP '+r.status);const d=await r.json();if(initialBrokerHoldings&&Array.isArray(d.broker_holdings)&&JSON.stringify(d.broker_holdings)!==JSON.stringify(initialBrokerHoldings)){location.reload();return}if(initialDashboardStatus&&d.data_status&&JSON.stringify(d.data_status)!==JSON.stringify(initialDashboardStatus)){location.reload();return}document.getElementById('footer-status').textContent='polling healthy · '+new Date().toLocaleTimeString()}catch(e){document.getElementById('footer-status').textContent='STALE / '+e.message;document.getElementById('footer-status').className='down'}}setTimeout(poll,30000);setInterval(poll,30000);render();</script></body></html>
'''

def _market_service(db: Database) -> MarketDataService:
    return MarketDataService(db, app.config.get("MARKET_TRANSPORTS", {}), stale_after_seconds=int(app.config.get("MARKET_STALE_AFTER_SECONDS", 900)))


@app.get("/api/market-data/status")
def api_market_data_status():
    db = Database(); db.init_db(); symbols = [row["symbol"] for row in db.list_holdings()]
    service = _market_service(db)
    statuses = service.status(symbols)
    return jsonify({"health": service.health(), "quotes": {key: {"available": value.available, "stale": value.stale, "age_seconds": value.age_seconds, "reason": value.reason, "source": value.quote.source if value.quote else None, "as_of": value.quote.as_of if value.quote else None} for key, value in statuses.items()}})


@app.get("/api/market-data/quotes")
def api_market_data_quotes():
    db = Database(); db.init_db(); rows = db.list_market_quotes(symbol=request.args.get("symbol"), source=request.args.get("source"), limit=min(int(request.args.get("limit", 100)), 500))
    return jsonify({"quotes": _json_rows(rows), "available": bool(rows)})


@app.post("/api/market-data/manual")
def api_market_data_manual():
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    payload = _payload()
    try:
        db = Database(); db.init_db(); quote = _market_service(db).manual_snapshot(str(payload["symbol"]), payload["price"], currency=str(payload.get("currency", "INR")), as_of=payload.get("as_of"), market_status=str(payload.get("market_status", "manual")))
        return jsonify({"quote": {"symbol": quote.symbol, "price": str(quote.price), "currency": quote.currency, "source": quote.source, "as_of": quote.as_of, "market_status": quote.market_status}}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.get("/api/benchmarks")
def api_benchmarks():
    db = Database(); db.init_db(); return jsonify({"benchmarks": _json_rows(db.list_benchmark_snapshots(benchmark=request.args.get("benchmark")))})


@app.post("/api/benchmarks")
def api_benchmark_snapshot():
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    payload = _payload()
    try:
        db = Database(); db.init_db(); item = BenchmarkSnapshot(str(payload["benchmark"]), payload["value"], payload.get("as_of") or _now(), "manual", str(payload.get("currency", "INR"))); db.save_benchmark_snapshot(item)
        return jsonify({"benchmark": item.benchmark, "value": str(item.value), "source": item.source, "as_of": item.as_of}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


def _manual_analytics_view(db: Database, *, portfolio: str | None = None, base_currency: str = "INR", source: str | None = None) -> dict[str, Any]:
    required = {(row["symbol"].upper(), row["currency"].upper()) for row in db.list_holdings(portfolio)}
    prices, seen = {}, set()
    now = datetime.now(timezone.utc)
    max_age = _market_service(db).stale_after_seconds
    for row in db.list_market_quotes(source=source, limit=500):
        key = (row["symbol"].upper(), row["currency"].upper())
        if key in seen: continue  # Newest recorded mark for this symbol/currency.
        seen.add(key)
        try:
            stamp = datetime.fromisoformat(row["as_of"].replace("Z", "+00:00"))
            if stamp.tzinfo is None: continue
            age = (now - stamp).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 <= age <= max_age:
            prices[key] = {"price": row["price"], "currency": key[1], "as_of": row["as_of"]}
    missing_keys = {key for key in required if key not in prices}
    return portfolio_analytics(db, portfolio=portfolio, base_currency=base_currency, prices=prices, stale_symbols=missing_keys)


@app.get("/api/analytics/portfolio")
def api_portfolio_analytics():
    db = Database(); db.init_db()
    result = _manual_analytics_view(db, portfolio=request.args.get("portfolio"), base_currency=request.args.get("currency", "INR"), source=request.args.get("source"))
    return jsonify({**result, "broker_holdings": _broker_holdings_view(db, _integration_status())})


@app.post("/api/stock-analysis/run")
def api_stock_analysis_run():
    payload = _payload()
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be an object"}), 400
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        return jsonify({"error": "symbol must be 1-20 letters, digits, dots, or hyphens"}), 400
    db = Database(); db.init_db()
    evidence = db.list_research_evidence(symbol=symbol, limit=500)
    facts = [
        {"evidence_id": int(row["id"]), "symbol": symbol, "statement": str(row["excerpt"])}
        for row in evidence if bool(row["verified"]) and str(row["excerpt"]).strip()
    ]
    try:
        run = run_agent(db, "market_research", payload={"topic": f"stock_analysis:{symbol}", "facts": facts})
    except PermissionError as exc:
        return jsonify({"error": str(exc), "agent": "market_research"}), 409
    response = {"agent": "market_research", "agent_run_id": run["id"], "status": run["status"], "result": run["result"]}
    return jsonify(response), 502 if run.get("status") == "failed" else 200


def _stock_symbol(value: str | None) -> str:
    symbol = str(value or "").strip().upper()
    if symbol and not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        raise ValueError("symbol must be 1-20 letters, digits, dots, or hyphens")
    return symbol


@app.get("/page/<section>")
def operator_page(section: str):
    if section not in {"trades", "ledger", "analytics", "agents", "research", "integrations", "strategies", "scheduler", "audit", "settings", "finance", "stock-analysis"}:
        return jsonify({"error": "unknown operator page"}), 404
    db = Database(); db.init_db()
    if section == "stock-analysis":
        try:
            symbol = _stock_symbol(request.args.get("symbol"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        manual = [dict(row) for row in db.list_holdings() if str(row["symbol"]).upper() == symbol] if symbol else []
        broker_snapshots = []
        if symbol:
            for group in _broker_holdings_view(db, _integration_status()):
                holdings = [row for row in group["holdings"] if str(row["symbol"]).upper() == symbol]
                positions = [row for row in group["positions"] if str(row["symbol"]).upper() == symbol]
                if holdings or positions:
                    broker_snapshots.append({**group, "holdings": holdings, "positions": positions})
        evidence = _json_rows(db.list_research_evidence(symbol=symbol, limit=100)) if symbol else []
        topic = f"stock_analysis:{symbol}"
        research_runs = db.list_research_runs(topic=topic, limit=1) if symbol else []
        research_run = research_runs[0] if research_runs else None
        findings = db.list_research_findings(run_id=int(research_run["id"])) if research_run else []
        data = {"symbol": symbol, "manual": manual, "broker_snapshots": broker_snapshots,
                "evidence": evidence, "research_run": research_run, "findings": findings}
    elif section == "trades": data = {"orders": _json_rows(db.list_order_intents()), "transactions": _json_rows(db.list_transactions())}
    elif section == "ledger":
        filters = {key: request.args.get(key) or None for key in ("namespace", "portfolio", "symbol", "as_of")}
        filters["namespace"] = filters["namespace"] or "manual"
        scope = {key: value for key, value in filters.items() if key != "as_of" and value is not None}
        events = _json_rows(db.list_financial_events(**scope))
        summary = calculate(db, namespace=filters["namespace"], portfolio=filters["portfolio"], symbol=filters["symbol"], as_of=filters["as_of"])
        query = "&".join(f"{key}={request.args.get(key)}" for key in ("namespace", "portfolio", "symbol") if request.args.get(key))
        suffix = f"&{query}" if query else f"&namespace={filters['namespace']}"
        data = {"filters": filters, "events": events, "lots": summary["lots"], "summary": summary, "export_json": "/api/ledger/export?format=json" + suffix, "export_csv": "/api/ledger/export?format=csv" + suffix}
    elif section == "analytics":
        data = {"manual_analytics": _manual_analytics_view(db, base_currency=str(load_settings().get("default_currency") or "INR")),
                "broker_holdings": _broker_holdings_view(db, _integration_status())}
    elif section == "agents": data = {"agents": list_agents(db)}
    elif section == "research": data = {"runs": db.list_research_runs(), "findings": db.list_research_findings(), "evidence": _json_rows(db.list_research_evidence())}
    elif section == "integrations": data = _integration_status()
    elif section == "scheduler": data = _scheduler_snapshot(db)
    elif section == "audit": data = {"kill_switch": db.get_console_state("global_kill_switch", "off"), "events": _json_rows(db.list_audit_events(limit=100)), "approvals": _json_rows(db.list_live_approvals())}
    elif section == "settings": data = load_settings()
    elif section == "finance": data = daily_report_data(db)
    else: data = {"paper_only": True, "supported": ["kite-short-term/v1", "upstox-long-term/v1", "indmoney-us-read-only/v1"], "note": "Strategies accept caller-supplied facts only and never submit orders."}
    descriptions = {"analytics": "Quote freshness, portfolio value, P/L, drawdown, concentration, and benchmark comparison from verified or manual snapshots.", "agents": "Enable, disable, test, run, retry, and inspect durable local agents.", "research": "Evidence, bounded findings, feedback, and paper-only decision support.", "trades": "Paper orders use the execution gateway; manual transactions remain local ledger records.", "ledger": "Import, reconcile, inspect FIFO cost basis, and export tax-ready ledger data.", "integrations": "Provider connectors are reported honestly from configuration only; no network calls are made.", "strategies": "Generate expiring proposals from supplied facts; proposals do not execute.", "scheduler": "Durable scheduler state and run outcomes.", "audit": "Append-only events, risk posture, kill switch, and live approval records.", "settings": "Local operator preferences and deployment posture.", "finance": "Goals, cash flow, liabilities, alerts, backups, corrections, and daily reports from local data.", "stock-analysis": "Ticker-specific, evidence-gated research with manual and broker snapshots kept separate."}
    return render_template_string(OPERATOR_PAGE_TEMPLATE, section=section, description=descriptions[section], data=data)


def _scheduler_snapshot(db: Database):
    with db.connect() as conn:
        state = _json_rows(conn.execute("SELECT * FROM scheduler_state ORDER BY trigger_id").fetchall())
        runs = _json_rows(conn.execute("SELECT * FROM scheduler_runs ORDER BY created_at DESC LIMIT 100").fetchall())
    return {"enabled": bool(load_settings().get("scheduler_enabled", False)), "state": state, "runs": runs}


@app.get("/api/agents")
def api_agents():
    db = Database(); db.init_db()
    return jsonify({"agents": list_agents(db), "paper_only": True})


@app.route("/api/agents/<name>", methods=["PUT", "PATCH"])
def api_agent_state(name: str):
    payload = _payload()
    db = Database(); db.init_db()
    if db.get_agent(name) is None: return jsonify({"error": "unknown agent"}), 404
    if not isinstance(payload.get("enabled"), bool): return jsonify({"error": "enabled must be a boolean"}), 400
    db.set_agent_enabled(name, payload["enabled"])
    return jsonify(next(item for item in list_agents(db) if item["name"] == name))


@app.post("/api/agents/<name>/run")
def api_agent_run(name: str):
    payload = _payload(); db = Database(); db.init_db()
    if any(key in payload and not isinstance(payload[key], bool) for key in ("test", "dry_run")): return jsonify({"error": "test and dry_run must be booleans"}), 400
    try: result = run_agent(db, name, run_type="test" if payload.get("test", payload.get("dry_run", False)) else "run", payload=payload)
    except KeyError: return jsonify({"error": "unknown agent"}), 404
    except PermissionError as exc: return jsonify({"error": str(exc), "code": "agent_disabled"}), 409
    return jsonify(result), 200 if result["status"] == "succeeded" else 500


@app.post("/api/agents/<name>/retry")
def api_agent_retry(name: str):
    db = Database(); db.init_db()
    try: result = retry_agent(db, name)
    except KeyError: return jsonify({"error": "unknown agent"}), 404
    except (PermissionError, ValueError) as exc: return jsonify({"error": str(exc)}), 409
    return jsonify(result), 200 if result["status"] == "succeeded" else 500


@app.get("/api/agents/<name>/runs")
def api_agent_runs(name: str):
    db = Database(); db.init_db()
    if db.get_agent(name) is None: return jsonify({"error": "unknown agent"}), 404
    return jsonify({"agent": name, "runs": db.list_agent_runs(name, min(int(request.args.get("limit", 50)), 100))})


@app.get("/api/agents/<name>/runs/<int:run_id>")
def api_agent_run_detail(name: str, run_id: int):
    db = Database(); db.init_db(); result = db.get_agent_run(run_id)
    if db.get_agent(name) is None: return jsonify({"error": "unknown agent"}), 404
    if result is None or result["agent_name"] != name: return jsonify({"error": "run not found"}), 404
    return jsonify(result)


@app.get("/api/portfolio")
def api_portfolio():
    db = Database(); db.init_db()
    snapshot, _, path = generate_report(db, save=False)
    return jsonify({"snapshot": snapshot, "broker_holdings": _broker_holdings_view(db, _integration_status()), "report_path": path})


@app.post("/api/research/evidence")
def api_research_evidence_create():
    try:
        payload = dict(_payload())
        # Public submissions are observations only; callers cannot self-certify them.
        payload["verified"] = False
        db = Database(); db.init_db(); evidence_id = add_evidence(db, payload)
        evidence = db.get_research_evidence(evidence_id)
        return jsonify({"id": evidence_id, "evidence": _json_rows([evidence])[0]}), 201
    except (TypeError, ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/research/evidence")
def api_research_evidence_list():
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    db = Database(); db.init_db()
    rows = db.list_research_evidence(symbol=request.args.get("symbol"), topic=request.args.get("topic"), limit=limit)
    return jsonify({"evidence": _json_rows(rows)})


@app.post("/api/research/run")
def api_research_run():
    payload = _payload()
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be an object"}), 400
    facts = payload.get("facts", [])
    if not isinstance(facts, list) or any(not isinstance(fact, dict) for fact in facts):
        return jsonify({"error": "facts must be an array of objects"}), 400
    try:
        db = Database(); db.init_db()
        result = run_research(db, topic=str(payload.get("topic", "market")), facts=facts, max_age_seconds=payload.get("max_age_seconds", 86400))
        return jsonify(result), 200
    except (TypeError, ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/research/runs")
def api_research_runs():
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    db = Database(); db.init_db(); return jsonify({"runs": db.list_research_runs(topic=request.args.get("topic"), limit=limit)})


@app.get("/api/research/findings")
def api_research_findings():
    try:
        limit = int(request.args.get("limit", 100))
        run_id = int(request.args["run_id"]) if request.args.get("run_id") else None
    except ValueError:
        return jsonify({"error": "limit and run_id must be integers"}), 400
    db = Database(); db.init_db(); return jsonify({"findings": db.list_research_findings(run_id=run_id, status=request.args.get("status"), limit=limit)})


@app.post("/api/research/findings/<int:finding_id>/feedback")
def api_research_feedback(finding_id: int):
    payload = _payload()
    try:
        db = Database(); db.init_db(); feedback_id = db.add_research_feedback(finding_id=finding_id, action=str(payload["action"]), correction=str(payload.get("correction", "")), user_id=str(session.get("operator", "operator")))
        return jsonify({"id": feedback_id, "finding_id": finding_id, "action": payload["action"]}), 201
    except (TypeError, ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/research/findings/<int:finding_id>/paper-proposal")
def api_research_paper_proposal(finding_id: int):
    db = Database(); db.init_db(); findings = [item for item in db.list_research_findings(limit=500) if int(item["id"]) == finding_id]
    if not findings: return jsonify({"error": "finding not found"}), 404
    finding = findings[0]
    if finding["status"] != "accepted": return jsonify({"error": "finding must be accepted before paper proposal generation"}), 409
    if not finding["evidence_ids"]: return jsonify({"error": "finding has no evidence"}), 409
    return jsonify({"proposal_id": f"research-{finding_id}", "mode": "paper", "execution": "disabled", "symbol": finding["symbol"], "rationale": finding["statement"], "evidence_ids": finding["evidence_ids"], "risks": finding["risks"], "invalidation": finding["invalidation"], "strategy_version": finding["strategy_version"]}), 201


@app.get("/api/research/portfolio-health")
def api_research_health():
    db = Database(); db.init_db(); return jsonify(portfolio_health(db, portfolio=request.args.get("portfolio")))


@app.get("/api/research/concentration-risk")
def api_research_concentration():
    db = Database(); db.init_db(); return jsonify(concentration_risk(db, portfolio=request.args.get("portfolio")))


@app.get("/api/research/<topic>")
def api_research_placeholder(topic: str):
    if topic not in {"rebalance-candidates", "earnings", "news"}: return jsonify({"error": "unknown research topic"}), 404
    return jsonify(placeholder(topic))


@app.get("/api/transactions")
def api_list_transactions():
    db = Database(); db.init_db()
    return jsonify({"transactions": _json_rows(db.list_transactions(int(request.args.get("limit", 100))))})


@app.post("/api/transactions")
def api_create_transaction():
    payload = _payload()
    try:
        db = Database(); db.init_db()
        db.adjust_holding(symbol=str(payload["symbol"]).strip().upper(), delta_shares=payload["shares"], trade_price=payload["price"], side=str(payload["side"]).lower(), fees=payload.get("fees", 0), trade_date=payload.get("trade_date"), notes=str(payload.get("notes", "")), portfolio=str(payload.get("portfolio", "Main")), currency=str(payload.get("currency", "INR")))
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    db.append_audit_event(event_type="manual.transaction.created", entity_type="transaction", entity_id=str(payload.get("symbol")), payload=payload, mode="paper")
    return jsonify({"status": "created", "transactions": _json_rows(db.list_transactions(1))}), 201


@app.route("/api/transactions/<int:transaction_id>", methods=["PUT", "PATCH", "DELETE"])
def api_transaction_mutation(transaction_id: int):
    return jsonify({"error": "transactions are append-only; create/read only", "transaction_id": transaction_id}), 405


@app.get("/api/ledger/events")
def api_ledger_events():
    db = Database(); db.init_db()
    scope = {k: request.args.get(k) for k in ("namespace", "portfolio", "symbol", "broker_id", "account_id") if request.args.get(k) is not None}
    return jsonify({"events": _json_rows(db.list_financial_events(**scope))})

@app.post("/api/ledger/import/preview")
@app.post("/api/ledger/import")
def api_ledger_import():
    if not _csrf_ok():
        return jsonify({"error": "CSRF validation failed"}), 403
    payload = _payload(); rows = payload.get("rows")
    if rows is None and request.files.get("file"):
        import csv, io
        rows = list(csv.DictReader(io.StringIO(request.files["file"].read().decode("utf-8"))))
    if not isinstance(rows, list): return jsonify({"error": "rows must be an array"}), 400
    db = Database(); db.init_db(); preview = request.path.endswith("/preview")
    try: result = import_rows(db, rows, namespace=str(payload.get("namespace","manual")), source=str(payload.get("source","import")), preview=preview)
    except (TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400
    if not preview: db.append_audit_event(event_type="ledger.import", entity_type="ledger_import", entity_id=str(result["accepted"]), payload=result, mode="paper")
    return jsonify(result), 200 if preview else 201

@app.get("/api/ledger/lots")
def api_ledger_lots():
    db = Database(); db.init_db()
    try: result = calculate(db, namespace=request.args.get("namespace","manual"), portfolio=request.args.get("portfolio"), symbol=request.args.get("symbol"), as_of=request.args.get("as_of"))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    return jsonify(result)

@app.get("/api/ledger/realized")
def api_ledger_realized():
    return api_ledger_lots()

@app.get("/api/ledger/income")
def api_ledger_income():
    db = Database(); db.init_db(); result=calculate(db, namespace=request.args.get("namespace","manual"), portfolio=request.args.get("portfolio"), as_of=request.args.get("as_of"))
    return jsonify({"income": result["income"], "fees": result["fees"], "taxes": result["taxes"]})

@app.get("/api/ledger/export")
def api_ledger_export():
    db = Database(); db.init_db(); fmt=request.args.get("format","json"); scope={k:request.args.get(k) for k in ("namespace","portfolio","symbol") if request.args.get(k) is not None}
    body=export_data(db, fmt=fmt, **scope); return (body, 200, {"Content-Type": "text/csv" if fmt == "csv" else "application/json"})

@app.get("/api/orders")
def api_list_orders():
    db = Database(); db.init_db()
    return jsonify({"orders": _json_rows(db.list_order_intents(request.args.get("status")))})


@app.post("/api/orders/paper")
def api_paper_order():
    payload = _payload()
    if str(payload.get("mode", "paper")).lower() == "live":
        return jsonify({"error": "LIVE orders are disabled; create an approval record instead"}), 409
    try:
        intent = OrderIntent(idempotency_key=str(payload["idempotency_key"]), portfolio_id=payload.get("portfolio_id", "Main"), account_id=payload.get("account_id"), symbol=str(payload["symbol"]), side=OrderSide(str(payload["side"]).lower()), order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, quantity=payload["quantity"], currency=str(payload.get("currency", "INR")), mode=ExecutionMode.PAPER, broker_id="paper")
        db = Database(); db.init_db()
        trading = load_settings()
        limits = RiskLimits(global_kill_switch=db.get_console_state("global_kill_switch", "off") == "on", max_order_notional=_decimal_or_none(trading.get("max_order_notional")), max_position_weight=_decimal_or_none(trading.get("max_position_weight")))
        gateway = ExecutionGateway(db, risk=RiskEngine(limits=limits, db=db))
        decision = gateway.submit(intent, quote_price=payload["quote_price"], portfolio_value=payload.get("portfolio_value"), current_position_notional=payload.get("current_position_notional", "0"))
        result = {"status": decision.status, "risk": {"allowed": decision.risk.allowed, "code": decision.risk.code, "message": decision.risk.message, "details": decision.risk.details}, "error": decision.error}
        if decision.broker_result: result["order"] = {"order_id": decision.broker_result.order_id, "status": decision.broker_result.status, "cash_balance": str(decision.broker_result.cash_balance), "total": str(decision.broker_result.total), "reused": decision.broker_result.reused}
        return jsonify(result), 200 if decision.status != "rejected" else 422
    except LiveModeRejected as exc:
        return jsonify({"error": str(exc), "live_enabled": False}), 409
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/integrations")
def api_integrations():
    return jsonify({"integrations": _integration_status()})


@app.post("/integrations/<name>/config")
@app.post("/api/integrations/<name>/config")
def integration_config(name: str):
    if name not in {"kite", "upstox", "indmoney"}:
        return jsonify({"error": "unknown integration"}), 404
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    payload = _payload()
    values = {key: str(payload[key]) for key in ("client_id", "account_id") if str(payload.get(key, "")).strip()}
    try: integration_auth.save_config(_integration_secret_key(), name, values)
    except (KeyError, RuntimeError) as exc: return jsonify({"error": str(exc)}), 500
    return redirect(url_for("operator_page", section="integrations")) if request.path.startswith("/integrations/") else jsonify(integration_auth.status(_integration_secret_key(), name))


@app.get("/integrations/<name>/start")
@app.post("/integrations/<name>/start")
@app.post("/api/integrations/<name>/connect")
def integration_start(name: str):
    if name not in integration_auth.PROVIDERS: return jsonify({"error": "unknown integration"}), 404
    if load_settings().get("provider_modes", {}).get(name, "mcp_only") != "mcp_only":
        return jsonify({"error": "provider REST/API transport is disabled; use mcp_only"}), 409
    if request.method == "POST" and not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    endpoint = OFFICIAL_MCP_ENDPOINTS.get(name)
    if not endpoint: return jsonify({"error": "official MCP endpoint is not configured"}), 503
    try:
        cfg = oauth_provider_config(name)
        metadata = integration_auth.fetch_metadata(endpoint["url"], metadata_url=cfg["metadata_url"])
        # Cloudflare Tunnel can forward HTTPS requests to the HTTP origin.
        # Register the externally visible callback, not the internal scheme.
        callback_uri = url_for("oauth_callback", name=name, _external=True,
                               _scheme="https" if os.environ.get("PORTFOLIO_PUBLIC_HTTPS") == "1" else request.scheme)
        client_id = integration_auth.get_provider(_integration_secret_key(), name).get("client_id")
        if not client_id and metadata.get("registration_endpoint"):
            registration = integration_auth._post_json(metadata["registration_endpoint"], {"client_name": "portfolio-tracker", "redirect_uris": [callback_uri], "token_endpoint_auth_method": "none"})
            client_id = registration.get("client_id")
            if not client_id: raise RuntimeError("dynamic client registration returned no client_id")
            integration_auth.save_config(_integration_secret_key(), name, {"client_id": str(client_id), "token_endpoint": str(metadata.get("token_endpoint"))})
        if not client_id: raise RuntimeError("no public OAuth client is configured and dynamic registration is unavailable")
        if not integration_auth.get_provider(_integration_secret_key(), name).get("token_endpoint"):
            integration_auth.save_config(_integration_secret_key(), name, {"token_endpoint": str(metadata["token_endpoint"])})
        target = integration_auth.begin_oauth(_integration_secret_key(), name, session_id=str(session.get("oauth_session_id", "")), redirect_uri=callback_uri, metadata=metadata, authorization_url=cfg["authorization_url"])
        return redirect(target)
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        return jsonify({"status": "authorization_required", "error": redact(str(exc)), "provider": name, "mcp_endpoint": endpoint["url"], "authorization_url": oauth_provider_config(name).get("authorization_url")}), 409


@app.get("/oauth/<name>/callback")
def oauth_callback(name: str):
    if name not in integration_auth.PROVIDERS: return jsonify({"error": "unknown OAuth provider"}), 404
    if request.args.get("error"): return jsonify({"error": "provider authorization was denied", "provider_error": request.args.get("error")}), 400
    state, code = request.args.get("state"), request.args.get("code")
    if not state or not code: return jsonify({"error": "OAuth callback state and code are required"}), 400
    record = integration_auth.get_provider(_integration_secret_key(), name)
    try:
        integration_auth.complete_oauth(_integration_secret_key(), name, state=state, code=code, session_id=str(session.get("oauth_session_id", "")), token_endpoint=str(record.get("token_endpoint") or ""))
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        return jsonify({"error": "provider OAuth callback failed", "detail": redact(str(exc))}), 400
    return redirect(url_for("operator_page", section="integrations"))


@app.post("/integrations/<name>/disconnect")
@app.post("/api/integrations/<name>/disconnect")
def integration_disconnect(name: str):
    if name not in integration_auth.PROVIDERS:
        return jsonify({"error": "unknown integration"}), 404
    if not _csrf_ok():
        return jsonify({"error": "CSRF validation failed"}), 403
    integration_auth.disconnect(_integration_secret_key(), name)
    return redirect(url_for("operator_page", section="integrations")) if request.path.startswith("/integrations/") else jsonify(integration_auth.status(_integration_secret_key(), name))


@app.get("/integrations/indmoney/connect")
def indmoney_connect():
    # Historical handoff URL: use the same current browser OAuth flow as UI.
    return redirect(url_for("integration_start", name="indmoney"))


@app.post("/api/integrations/<name>/sync")
def api_integration_sync(name: str):
    if name not in integration_auth.PROVIDERS:
        return jsonify({"error": "unknown integration"}), 404
    if not _csrf_ok():
        return jsonify({"error": "CSRF validation failed"}), 403
    try:
        db = Database(); db.init_db(); adapter = _provider_adapter(name); result = ProviderSyncService(db).sync(adapter)
        if result["status"] == "succeeded":
            record = integration_auth.get_provider(_integration_secret_key(), name)
            integration_auth.mark_mcp_verified(_integration_secret_key(), name, session_id=str(record.get("oauth_session") or ""), tools=sorted(adapter.client.tools))
        if result.get("error", {}).get("code") == "mcp_authorization_required":
            return jsonify({"integration": name, "status": "authorization_required", "error": result["error"], "authorization_url": adapter.client.authorization_url, "live_writes": False}), 409
        return jsonify({"integration": name, **result}), 200 if result["status"] == "succeeded" else 502
    except MCPAuthorizationRequired as exc:
        return jsonify({"integration": name, "status": "authorization_required", "error": {"code": exc.code, "message": exc.message, "retryable": False}, "authorization_url": exc.authorization_url, "live_writes": False}), 409
    except (KeyError, ValueError, RuntimeError, BrokerAdapterError) as exc:
        return jsonify({"integration": name, "status": "error", "error": {"code": "connector_unconfigured", "message": redact(str(exc)), "retryable": False}}), 409


@app.get("/api/integrations/<name>/sync")
def api_integration_sync_status(name: str):
    if name not in integration_auth.PROVIDERS: return jsonify({"error": "unknown integration"}), 404
    record = integration_auth.get_provider(_integration_secret_key(), name)
    account_id = str(record.get("account_id") or load_settings().get("default_account_id") or "operator")
    db = Database(); db.init_db()
    runs = _json_rows(db.list_provider_sync_runs(broker_id=name, account_id=account_id))
    reconciliations = _json_rows(db.list_provider_reconciliations(broker_id=name, account_id=account_id))
    return jsonify({"integration": name, "account_id": account_id, "status": _integration_status()[name], "runs": runs, "reconciliations": reconciliations, "live_writes": False})


@app.get("/api/integrations/<name>/snapshot")
def api_integration_snapshot(name: str):
    if name not in integration_auth.PROVIDERS: return jsonify({"error": "unknown integration"}), 404
    record = integration_auth.get_provider(_integration_secret_key(), name)
    account_id = str(record.get("account_id") or load_settings().get("default_account_id") or "operator")
    db = Database(); db.init_db()
    return jsonify({"integration": name, "account_id": account_id, "read_only": True, "live_writes": False, "holdings": _json_rows(db.list_provider_holdings(broker_id=name, account_id=account_id)), "positions": _json_rows(db.list_provider_positions(broker_id=name, account_id=account_id)), "cash": _json_rows(db.list_provider_cash(broker_id=name, account_id=account_id)), "orders": _json_rows(db.list_provider_orders(broker_id=name, account_id=account_id)), "fills": _json_rows(db.list_provider_fills(broker_id=name, account_id=account_id))})


@app.post("/api/integrations/indmoney/write")
def indmoney_write_rejected():
    return jsonify({"error": "INDmoney MCP is read-only; trading and account mutations are unsupported", "live_writes": False}), 405


@app.get("/api/strategies")
def api_strategies():
    return jsonify({"strategies": ["kite-short-term/v1", "upstox-long-term/v1", "indmoney-us-read-only/v1"], "execution": "proposals only"})


@app.post("/api/strategies/proposals")
def api_strategy_proposal():
    payload = _payload()
    if str(payload.get("mode", "paper")).lower() == "live":
        return jsonify({"error": "live proposals require an explicit approval record and are not executable"}), 409
    try:
        now = datetime.fromisoformat(str(payload.get("now") or datetime.now(timezone.utc).isoformat()))
        fact = MarketFact(str(payload["symbol"]), str(payload["market"]), str(payload["currency"]), payload["price"], datetime.fromisoformat(str(payload["as_of"])), str(payload.get("source_id", "operator-input")), str(payload.get("signal", "hold")), payload.get("quantity", "1"), tuple(payload.get("evidence", [])))
        portfolio = PortfolioFact(str(payload.get("portfolio_id", "Main")), str(payload.get("account_id", "operator")), str(payload.get("broker_id", "kite")), fact.market, fact.currency)
        strategy = str(payload.get("strategy", "kite-short-term/v1"))
        if strategy == "kite-short-term/v1": result = propose_kite_short_term(fact, portfolio, now=now)
        elif strategy == "upstox-long-term/v1": result = propose_upstox_long_term(fact, portfolio, now=now)
        elif strategy == "indmoney-us-read-only/v1": result = propose_indmoney_us_tracking(fact, PortfolioFact(portfolio.portfolio_id, portfolio.account_id, "indmoney", "US", "USD"), now=now)
        else: return jsonify({"error": "unknown strategy"}), 400
        db = Database(); db.init_db(); db.append_audit_event(event_type="strategy.proposal.generated", entity_type="proposal", entity_id=str(getattr(result, "strategy", strategy)), payload=result.to_dict(), broker_id=portfolio.broker_id, account_id=portfolio.account_id, portfolio_id=portfolio.portfolio_id, mode="paper")
        return jsonify(result.to_dict())
    except (KeyError, TypeError, ValueError, StrategyInputError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/scheduler")
def api_scheduler():
    return jsonify(_scheduler_snapshot(Database()))


@app.post("/api/scheduler/state")
def api_scheduler_state():
    payload = _payload(); value = str(payload.get("enabled", "")).lower()
    if value not in {"true", "false", "on", "off"}: return jsonify({"error": "enabled must be boolean"}), 400
    enabled = value in {"true", "on"}
    settings = load_settings(); settings["scheduler_enabled"] = enabled; save_settings(settings)
    db = Database(); db.init_db(); db.set_console_state("scheduler_enabled", "true" if enabled else "false")
    db.append_audit_event(event_type="scheduler.state.changed", entity_type="scheduler", entity_id="operator", payload={"enabled": enabled}, mode="paper")
    return jsonify({"enabled": enabled})


@app.post("/api/scheduler/run")
def api_scheduler_run():
    db = Database(); db.init_db(); db.append_audit_event(event_type="scheduler.manual_run", entity_type="scheduler", entity_id="operator", payload={"requested": True}, mode="paper")
    return jsonify({"status": "accepted", "generated": 0, "message": "No external strategy factories are registered by the console; no provider call was made."})


@app.get("/api/audit")
def api_audit():
    db = Database(); db.init_db()
    return jsonify({"events": _json_rows(db.list_audit_events(limit=200)), "approvals": _json_rows(db.list_live_approvals())})


@app.get("/api/risk")
def api_risk():
    db = Database(); db.init_db()
    settings = load_settings()
    return jsonify({"paper_only": True, "live_writes": False, "trading_mode": settings.get("trading_mode"), "global_kill_switch": db.get_console_state("global_kill_switch", "off") == "on", "max_order_notional": settings.get("max_order_notional"), "max_position_weight": settings.get("max_position_weight"), "scheduler_enabled": settings.get("scheduler_enabled")})


@app.post("/api/risk/kill-switch")
def api_kill_switch():
    payload = _payload(); value = str(payload.get("state", "")).lower()
    if value not in {"on", "off"}: return jsonify({"error": "state must be on or off"}), 400
    db = Database(); db.init_db(); db.set_console_state("global_kill_switch", value); db.append_audit_event(event_type="risk.kill_switch.changed", entity_type="risk", entity_id="global", payload={"state": value}, mode="paper")
    return jsonify({"global_kill_switch": value == "on"})


@app.get("/api/approvals")
def api_approvals():
    db = Database(); db.init_db(); return jsonify({"approvals": _json_rows(db.list_live_approvals(request.args.get("status")))})


@app.post("/api/approvals")
def api_create_approval():
    payload = _payload(); proposal = payload.get("proposal", payload)
    if not isinstance(proposal, dict): return jsonify({"error": "proposal must be an object"}), 400
    if str(proposal.get("mode", "live")).lower() != "live": return jsonify({"error": "approval records are only for future LIVE proposals"}), 400
    proposal_id = str(payload.get("proposal_id") or proposal.get("proposal_id") or "")
    if not proposal_id: return jsonify({"error": "proposal_id is required"}), 400
    db = Database(); db.init_db()
    try: approval_id = db.create_live_approval(proposal_id=proposal_id, proposal_payload=proposal, requested_by=str(session.get("operator", os.environ.get("PORTFOLIO_ADMIN_USERNAME", "operator"))))
    except (ValueError, Exception) as exc:
        if "UNIQUE" in str(exc): return jsonify({"error": "proposal already has an approval record"}), 409
        return jsonify({"error": str(exc)}), 400
    db.append_audit_event(event_type="live.approval.requested", entity_type="approval", entity_id=str(approval_id), payload={"proposal_id": proposal_id}, mode="live")
    return jsonify({"id": approval_id, "status": "pending", "execution": "disabled"}), 201


@app.post("/api/approvals/<int:approval_id>/decision")
def api_decide_approval(approval_id: int):
    payload = _payload(); status = str(payload.get("status", ""))
    db = Database(); db.init_db()
    try: changed = db.decide_live_approval(approval_id, status=status, decided_by=str(session.get("operator", os.environ.get("PORTFOLIO_ADMIN_USERNAME", "operator"))), note=str(payload.get("note", "")))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    if not changed: return jsonify({"error": "approval not found or already decided"}), 404
    db.append_audit_event(event_type="live.approval.decided", entity_type="approval", entity_id=str(approval_id), payload={"status": status}, mode="live")
    return jsonify({"id": approval_id, "status": status, "execution": "still disabled"})


@app.get("/api/finance/goals")
def api_finance_goals():
    db = Database(); db.init_db()
    return jsonify({"goals": list_goals(db, account_id=request.args.get("account_id"), portfolio_id=request.args.get("portfolio_id"), namespace=request.args.get("namespace"))})


@app.post("/api/finance/goals")
def api_finance_goal_create():
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try:
        db = Database(); db.init_db(); goal_id = create_goal(db, _payload()); return jsonify({"id": goal_id, "goals": list_goals(db)}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.post("/api/finance/goals/<int:goal_id>/contributions")
def api_finance_goal_contribution(goal_id: int):
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try:
        payload = _payload(); db = Database(); db.init_db(); contribution_id = add_goal_contribution(db, goal_id, payload["amount"], contributed_at=payload.get("contributed_at"), source=str(payload.get("source", "manual")), provenance=payload.get("provenance")); return jsonify({"id": contribution_id, "goals": list_goals(db)}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.route("/api/finance/cash-flow", methods=["GET", "POST"])
def api_finance_cash_flow():
    db = Database(); db.init_db()
    if request.method == "GET":
        clauses=[]; params=[]
        for key in ("namespace", "account_id", "portfolio_id"):
            if request.args.get(key) is not None: clauses.append(key+"=?"); params.append(request.args[key])
        with db.connect() as c: rows = [dict(r) for r in c.execute("SELECT * FROM cash_flow_records" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY occurred_at DESC, id DESC", params).fetchall()]
        return jsonify({"cash_flow": rows})
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try: return jsonify({"id": add_cash_flow(db, _payload())}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.route("/api/finance/liabilities", methods=["GET", "POST"])
def api_finance_liabilities():
    db = Database(); db.init_db()
    if request.method == "GET":
        clauses=[]; params=[]
        for key in ("namespace", "account_id", "portfolio_id"):
            if request.args.get(key) is not None: clauses.append(key+"=?"); params.append(request.args[key])
        with db.connect() as c: rows = [dict(r) for r in c.execute("SELECT * FROM liabilities" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY due_date, id", params).fetchall()]
        return jsonify({"liabilities": rows})
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try: return jsonify({"id": add_liability(db, _payload())}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.route("/api/finance/alerts", methods=["GET", "POST"])
def api_finance_alerts():
    db = Database(); db.init_db()
    if request.method == "GET":
        clauses=[]; params=[]
        for key in ("namespace", "account_id", "portfolio_id", "status"):
            if request.args.get(key) is not None: clauses.append(key+"=?"); params.append(request.args[key])
        with db.connect() as c: rows = [dict(r) for r in c.execute("SELECT * FROM alerts" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC", params).fetchall()]
        return jsonify({"alerts": rows})
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try: return jsonify({"id": create_alert(db, _payload())}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.post("/api/finance/alerts/<int:alert_id>/acknowledge")
def api_finance_alert_ack(alert_id: int):
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    db = Database(); db.init_db()
    if not acknowledge_alert(db, alert_id, actor=str(session.get("operator", "operator"))): return jsonify({"error": "alert not found or already acknowledged"}), 404
    return jsonify({"id": alert_id, "status": "acknowledged"})


@app.get("/api/finance/daily-report")
def api_finance_daily_report():
    db = Database(); db.init_db(); return jsonify(daily_report_data(db))


@app.route("/api/finance/backup", methods=["GET", "POST"])
def api_finance_backup():
    db = Database(); db.init_db()
    if request.method == "GET": return jsonify(backup_payload(db))
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    path = _payload().get("path")
    if not path: return jsonify({"error": "path is required"}), 400
    try: return jsonify(write_backup(db, path)), 201
    except (OSError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.post("/api/finance/backup/restore")
def api_finance_backup_restore():
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    path = _payload().get("path")
    if not path: return jsonify({"error": "path is required"}), 400
    try: db = Database(); db.init_db(); return jsonify({"restored": restore_backup(db, path)})
    except (OSError, ValueError, json.JSONDecodeError) as exc: return jsonify({"error": str(exc)}), 400


@app.get("/api/finance/alerts/<int:alert_id>/deliveries")
def api_finance_alert_deliveries(alert_id: int):
    db = Database(); db.init_db(); return jsonify({"deliveries": list_alert_deliveries(db, alert_id)})


@app.post("/api/finance/alerts/<int:alert_id>/deliveries")
def api_finance_alert_delivery_update(alert_id: int):
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try:
        payload = _payload(); db = Database(); db.init_db(); set_alert_delivery(db, alert_id, str(payload["channel"]), state=str(payload.get("state", "delivered")), error=str(payload.get("error", "")), read=bool(payload.get("read", False))); return jsonify({"deliveries": list_alert_deliveries(db, alert_id)})
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.get("/api/finance/transactions/<int:transaction_id>/corrections")
def api_finance_transaction_corrections(transaction_id: int):
    db = Database(); db.init_db()
    with db.connect() as c: rows = [dict(r) for r in c.execute("SELECT * FROM transaction_corrections WHERE transaction_id=? ORDER BY id", (transaction_id,)).fetchall()]
    return jsonify({"corrections": rows})


@app.post("/api/finance/transactions/<int:transaction_id>/reversal")
def api_finance_transaction_reversal(transaction_id: int):
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try: db = Database(); db.init_db(); return jsonify({"id": correct_transaction(db, transaction_id, {"reversal_of": transaction_id, "reason": str(_payload().get("reason", "manual reversal"))}, actor=str(session.get("operator", "operator")))}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


@app.post("/api/finance/transactions/<int:transaction_id>/correction")
def api_finance_transaction_correction(transaction_id: int):
    if not _csrf_ok(): return jsonify({"error": "CSRF validation failed"}), 403
    try: db = Database(); db.init_db(); return jsonify({"id": correct_transaction(db, transaction_id, _payload(), actor=str(session.get("operator", "operator")))}), 201
    except (KeyError, TypeError, ValueError) as exc: return jsonify({"error": str(exc)}), 400


def main(host: str = "127.0.0.1", port: int = 5055, debug: bool = False):
    db = Database(); db.init_db()
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

# Phase 6: durable promotion-gate API and local MCP Streamable HTTP endpoint.
@app.get("/api/promotion-gates")
def api_promotion_gates():
    return jsonify(PromotionGates(Database()).status())

@app.post("/api/promotion-gates/<gate_key>")
def api_record_promotion_gate(gate_key: str):
    payload = _payload()
    if not _csrf_ok():
        return jsonify({"error": "CSRF token required"}), 403
    try:
        gate = PromotionGates(Database()).record(gate_key, status=str(payload.get("status", "unknown")), evidence=str(payload.get("evidence", "")), reviewer=str(payload.get("reviewer", "")), reviewed_at=payload.get("reviewed_at"), actor=str(session.get("operator_authenticated")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(gate)

@app.get("/promotion-gates")
def promotion_gates_page():
    state = PromotionGates(Database()).status()
    rows = "".join(f"<tr><td>{g['label']}</td><td>{g['status']}</td><td>{g.get('reviewer','')}</td><td>{g.get('reviewed_at') or ''}</td></tr>" for g in state["gates"])
    return render_template_string("<h1>Phase 6 promotion gates</h1><p>Ready: {{ ready }}</p><table><tr><th>Gate</th><th>Status</th><th>Reviewer</th><th>Timestamp</th></tr>" + rows + "</table>", ready=state["ready"])

@app.post("/mcp")
def mcp_endpoint():
    if not token_valid(request) and not session.get("operator_authenticated"):
        return jsonify({"jsonrpc":"2.0", "id": (request.get_json(silent=True) or {}).get("id"), "error":{"code":-32001,"message":"MCP authentication required"}}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return jsonify({"error":"JSON-RPC 2.0 request required"}), 400
    response = MCPServer().rpc(payload, actor="mcp-token" if token_valid(request) else "operator-session")
    if response is None: return ("", 202)
    return jsonify(redact(response))
