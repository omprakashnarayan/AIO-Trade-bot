from __future__ import annotations

import ipaddress
import json
import math
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash

from .config import DEFAULT_SETTINGS, load_settings, save_settings
from .agents import AGENTS, list_agents, retry_agent, run_agent
from .db import Database
from .execution import ExecutionGateway, LiveModeRejected
from .reporting import generate_report
from .risk import RiskEngine, RiskLimits
from .strategies import MarketFact, PortfolioFact, StrategyInputError, propose_kite_short_term, propose_upstox_long_term, propose_indmoney_us_tracking
from .trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce

app = Flask(__name__)
app.secret_key = os.environ.get("PORTFOLIO_SESSION_SECRET") or secrets.token_hex(32)


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
    if not _credentials_configured():
        if request.path.startswith("/api/"):
            return jsonify({"error": "admin credentials are not configured; console is locked", "setup_required": True}), 503
        return redirect(url_for("setup"))
    if not session.get("operator_authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("login", next=request.path))
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
            session.clear(); session["operator_authenticated"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid credentials"
    return render_template_string(LOGIN_TEMPLATE, error=error), 401 if error else 200


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "auth_configured": _credentials_configured(), "paper_only": True})

action_template = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PORTFOLIO // NOC</title>
  <style>
    :root { color-scheme: dark; --bg:#080d14; --panel:#0e1723; --panel2:#121e2d; --line:#203247; --text:#dce8f4; --muted:#8498ad; --cyan:#43d9ff; --green:#5ee6a8; --amber:#f5c66d; --red:#ff7184; }
    * { box-sizing:border-box; } body { margin:0; min-height:100vh; background:radial-gradient(circle at 80% -20%,#15324c 0,transparent 40%),var(--bg); color:var(--text); font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif; }
    a { color:var(--cyan); text-decoration:none; } a:hover { text-decoration:underline; } .shell { max-width:1500px; margin:auto; padding:28px clamp(16px,3vw,44px) 50px; }
    .topbar { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:30px; } .brand { display:flex; align-items:center; gap:13px; letter-spacing:.16em; font-weight:800; }
    .mark { width:34px; height:34px; border:1px solid var(--cyan); color:var(--cyan); display:grid; place-items:center; border-radius:8px; box-shadow:0 0 24px #43d9ff30; } .eyebrow { color:var(--muted); text-transform:uppercase; letter-spacing:.16em; font-size:11px; }
    .top-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; } .pill { border:1px solid var(--line); padding:6px 10px; border-radius:99px; color:var(--muted); font-size:12px; } .pill.live { color:var(--green); border-color:#2c6c54; }
    h1,h2,h3,p { margin-top:0; } h1 { font-size:clamp(28px,4vw,48px); line-height:1.05; letter-spacing:-.04em; margin-bottom:10px; } h2 { font-size:17px; letter-spacing:.02em; } h3 { font-size:13px; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); }
    .hero { display:flex; justify-content:space-between; align-items:end; gap:24px; margin-bottom:22px; } .hero-copy { max-width:720px; } .muted { color:var(--muted); } .grid { display:grid; grid-template-columns:repeat(12,1fr); gap:16px; } .span-3 { grid-column:span 3; } .span-4 { grid-column:span 4; } .span-6 { grid-column:span 6; } .span-8 { grid-column:span 8; } .span-12 { grid-column:span 12; }
    .card { background:linear-gradient(145deg,#111d2bdb,#0b131ecc); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 14px 40px #00000025; } .card-head { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:15px; } .card-head h2 { margin:0; }
    .metric { min-height:126px; position:relative; overflow:hidden; } .metric:after { content:""; position:absolute; width:100px; height:100px; border:1px solid #43d9ff30; border-radius:50%; right:-35px; bottom:-50px; } .metric .value { font-size:28px; font-weight:750; margin:7px 0; } .accent { color:var(--cyan); } .good { color:var(--green); } .warn { color:var(--amber); }
    table { width:100%; border-collapse:collapse; } th { color:var(--muted); text-transform:uppercase; letter-spacing:.1em; font-size:10px; font-weight:600; } th,td { text-align:left; padding:12px 10px; border-bottom:1px solid #1b2a3c; vertical-align:middle; } tbody tr:hover { background:#14233766; } .table-wrap { overflow:auto; }
    .symbol { color:var(--cyan); font-weight:700; letter-spacing:.04em; } .sub { display:block; color:var(--muted); font-size:12px; } .empty { padding:22px 0; color:var(--muted); text-align:center; }
    form { display:grid; gap:10px; } label { color:var(--muted); font-size:12px; } input,select,textarea { width:100%; border:1px solid var(--line); border-radius:8px; background:#09121d; color:var(--text); padding:10px 11px; outline:none; } input:focus,select:focus,textarea:focus { border-color:var(--cyan); box-shadow:0 0 0 3px #43d9ff15; } .form-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; } .full { grid-column:1/-1; }
    button,.button { border:1px solid #2b6277; border-radius:8px; background:#123344; color:#dff8ff; padding:10px 13px; cursor:pointer; font-weight:700; } button:hover,.button:hover { background:#17465b; text-decoration:none; } button.danger { color:#ffb4bd; border-color:#6b3040; background:#321a25; } button.small { padding:5px 8px; font-size:11px; } .actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .integration { display:flex; gap:12px; align-items:flex-start; padding:13px 0; border-bottom:1px solid #1b2a3c; } .integration:last-child { border-bottom:0; padding-bottom:0; } .status { width:8px; height:8px; border-radius:50%; margin-top:7px; background:var(--amber); box-shadow:0 0 12px currentColor; } .status.off { background:var(--red); } .integration strong { display:block; } .tag { display:inline-block; font-size:10px; text-transform:uppercase; letter-spacing:.08em; padding:3px 6px; border-radius:4px; margin-top:4px; color:var(--amber); background:#3b2d16; } .tag.safe { color:var(--green); background:#143226; }
    .notice { border-left:3px solid var(--amber); background:#2a2111; color:#e9d3a5; padding:11px 13px; border-radius:5px; font-size:12px; } .section-space { margin-top:16px; } footer { margin-top:28px; color:var(--muted); font-size:11px; display:flex; justify-content:space-between; gap:15px; flex-wrap:wrap; }
    @media (max-width:950px) { .span-3,.span-4,.span-6,.span-8 { grid-column:span 6; } .hero { align-items:start; flex-direction:column; } }
    @media (max-width:620px) { .span-3,.span-4,.span-6,.span-8,.span-12 { grid-column:1/-1; } .topbar { align-items:flex-start; flex-direction:column; } .form-grid { grid-template-columns:1fr; } .full { grid-column:auto; } }
  </style>
</head>
<body>
<div class="shell">
  <header class="topbar"><div class="brand"><div class="mark">⌁</div><div>PORTFOLIO <span class="muted">// NOC</span></div></div><div class="top-actions"><span class="pill live">● LOCAL / PAPER MODE</span><a class="button" href="{{ url_for('refresh') }}">↻ Refresh snapshot</a><a href="{{ url_for('api_report') }}">JSON report ↗</a><form method="post" action="{{ url_for('logout') }}" style="display:inline"><button class="small">Logout</button></form></div></header>
  <nav class="actions" aria-label="Operator navigation"><a class="button small" href="{{ url_for('index') }}">Portfolio</a><a class="button small" href="{{ url_for('operator_page', section='agents') }}">Agents</a><a class="button small" href="{{ url_for('operator_page', section='trades') }}">Trades</a><a class="button small" href="{{ url_for('operator_page', section='integrations') }}">Integrations</a><a class="button small" href="{{ url_for('operator_page', section='strategies') }}">Strategies</a><a class="button small" href="{{ url_for('operator_page', section='scheduler') }}">Scheduler</a><a class="button small" href="{{ url_for('operator_page', section='audit') }}">Audit / Risk</a><a class="button small" href="{{ url_for('operator_page', section='settings') }}">Settings</a></nav>
  <section class="hero"><div class="hero-copy"><div class="eyebrow">Homelab portfolio operations console</div><h1>Signal over noise.</h1><p class="muted">A local-first command center for holdings, price snapshots, and disciplined paper-trading workflows.</p></div><div class="eyebrow">Last scan: {{ snapshot_time }}</div></section>

  <section class="grid">
    <article class="card metric span-3"><div class="eyebrow">Portfolios tracked</div><div class="value accent">{{ summary.portfolios }}</div><div class="muted">isolated local books</div></article>
    <article class="card metric span-3"><div class="eyebrow">Open positions</div><div class="value">{{ summary.positions }}</div><div class="muted">manual holdings</div></article>
    {% set total_value = portfolios|sum(attribute='summary.total_value') %}{% set total_cost = portfolios|sum(attribute='summary.total_cost') %}
    <article class="card metric span-3"><div class="eyebrow">Tracked value</div><div class="value">{{ money(total_value, portfolios[0].currency if portfolios else 'INR') }}</div><div class="muted">cached / fetched prices</div></article>
    <article class="card metric span-3"><div class="eyebrow">Runtime posture</div><div class="value good">SAFE</div><div class="muted">broker writes disabled</div></article>

    <article class="card span-8"><div class="card-head"><h2>Position telemetry</h2><span class="eyebrow">{{ summary.positions }} records</span></div>
      {% if portfolios %}{% for section in portfolios %}<div class="section-space"><div class="card-head"><div><h3>{{ section.portfolio }} / {{ section.currency }}</h3><span class="muted">{{ section.summary.positions }} positions · value {{ money(section.summary.total_value, section.currency) }}</span></div><span class="{{ 'good' if section.summary.gain >= 0 else 'warn' }}">{{ money(section.summary.gain, section.currency) }} ({{ pct(section.summary.gain_pct) }})</span></div>
      <div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Shares</th><th>Avg cost</th><th>Mark</th><th>Value</th><th>Weight</th><th></th></tr></thead><tbody>
      {% for h in section.holdings %}<tr><td><span class="symbol">{{ h.symbol }}</span><span class="sub">{{ h.label or 'manual position' }}</span></td><td>{{ '%.4f'|format(h.shares) }}</td><td>{{ money(h.avg_cost, section.currency) }}</td><td>{{ money(h.current_price, section.currency) if h.current_price is not none else 'n/a' }}<span class="sub">{{ h.source }}</span></td><td>{{ money(h.market_value, section.currency) }}</td><td>{{ pct(h.weight * 100) }}</td><td>{% if h.shares|float == 0 %}<button class="small danger" data-delete data-portfolio="{{ section.portfolio }}" data-symbol="{{ h.symbol }}">Delete</button>{% else %}<span class="eyebrow">locked</span>{% endif %}</td></tr>{% endfor %}
      </tbody></table></div></div>{% endfor %}{% else %}<div class="empty">No holdings yet. Add a local position to start the console.</div>{% endif %}
    </article>

    <article class="card span-4"><div class="card-head"><h2>Integrations</h2><span class="pill">LOCAL ONLY</span></div><div class="notice">Credentials and provider MCP endpoints are not configured. This panel is a capability map, not a connectivity claim.</div>
      <div class="integration"><span class="status off"></span><div><strong>Kite</strong><span class="muted">Paper broker / read-only adapters</span><br><span class="tag safe">live order writes disabled</span></div></div>
      <div class="integration"><span class="status off"></span><div><strong>Upstox</strong><span class="muted">Paper broker / read-only adapters</span><br><span class="tag safe">live order writes disabled</span></div></div>
      <div class="integration"><span class="status"></span><div><strong>INDmoney</strong><span class="muted">Imported holdings visibility</span><br><span class="tag">read-only</span></div></div>
    </article>

    <article class="card span-4"><div class="card-head"><h2>Local settings</h2><span class="eyebrow">JSON-backed</span></div><form method="post" action="{{ url_for('update_settings') }}"><div class="form-grid"><div><label>Owner name</label><input name="owner_name" value="{{ settings.owner_name }}"></div><div><label>Risk profile</label><select name="risk_profile">{% for option in ['low','balanced','aggressive'] %}<option value="{{ option }}" {% if settings.risk_profile == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></div><div><label>Monthly contribution</label><input name="monthly_contribution" value="{{ settings.monthly_contribution }}"></div><div><label>Reminder time</label><input name="reminder_time" value="{{ settings.reminder_time }}" placeholder="08:00"></div><div class="full"><label>Notes</label><textarea name="notes" rows="3">{{ settings.notes }}</textarea></div></div><button type="submit">Save settings</button></form></article>
    <article class="card span-4"><div class="card-head"><h2>Manual holding</h2><span class="eyebrow">local CRUD</span></div><form method="post" action="{{ url_for('add_holding') }}"><div class="form-grid"><div><label>Portfolio</label><input name="portfolio" value="Main"></div><div><label>Currency</label><input name="currency" value="INR"></div><div><label>Symbol</label><input name="symbol" required placeholder="INFY"></div><div><label>Label</label><input name="label" placeholder="Optional"></div><div><label>Shares</label><input name="shares" type="number" step="0.0001" required></div><div><label>Average cost</label><input name="avg_cost" type="number" step="0.01" required></div><div class="full"><label>Notes</label><input name="notes"></div></div><button type="submit">Save holding</button></form></article>
    <article class="card span-4"><div class="card-head"><h2>Manual transaction</h2><span class="eyebrow">paper ledger</span></div><form method="post" action="{{ url_for('add_transaction') }}"><div class="form-grid"><div><label>Portfolio</label><input name="portfolio" value="Main"></div><div><label>Currency</label><input name="currency" value="INR"></div><div><label>Symbol</label><input name="symbol" required></div><div><label>Side</label><select name="side"><option>buy</option><option>sell</option></select></div><div><label>Shares</label><input name="shares" type="number" step="0.0001" required></div><div><label>Price</label><input name="price" type="number" step="0.01" required></div><div><label>Fees</label><input name="fees" type="number" step="0.01" value="0"></div><div><label>Trade date</label><input name="trade_date" placeholder="YYYY-MM-DD"></div><div class="full"><label>Notes</label><input name="notes"></div></div><button type="submit">Record transaction</button></form></article>

    <article class="card span-12"><div class="card-head"><h2>Recent ledger events</h2><span class="eyebrow">latest 25</span></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Portfolio</th><th>Symbol</th><th>Side</th><th>Shares</th><th>Price</th><th>Fees</th><th>Notes</th></tr></thead><tbody>{% for t in transactions %}<tr><td>{{ t.trade_date }}</td><td>{{ t.portfolio }}</td><td class="symbol">{{ t.symbol }}</td><td>{{ t.side }}</td><td>{{ t.shares }}</td><td>{{ money(t.price,t.currency) }}</td><td>{{ money(t.fees,t.currency) }}</td><td class="muted">{{ t.notes }}</td></tr>{% else %}<tr><td colspan="8" class="empty">No transactions recorded.</td></tr>{% endfor %}</tbody></table></div></article>
  </section>
  <footer><span>PORTFOLIO // NOC · local-first operations</span><span>Paper-only safety boundary · no live order routes</span></footer>
</div>
<script>
  document.querySelectorAll('[data-delete]').forEach((button) => button.addEventListener('click', async () => {
    const portfolio = button.dataset.portfolio, symbol = button.dataset.symbol;
    if (!confirm(`Delete zero-share holding ${symbol} from ${portfolio}?`)) return;
    const response = await fetch(`/api/holdings/${encodeURIComponent(portfolio)}/${encodeURIComponent(symbol)}`, {method:'DELETE'});
    const data = await response.json();
    if (!response.ok) { alert(data.error || 'Delete rejected'); return; }
    window.location.reload();
  }));
</script>
</body></html>
"""

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
    return {"money": _money, "pct": _pct}


@app.get("/")
def index():
    snapshot, _, _ = generate_report(Database(), save=True)
    return render_template_string(action_template, summary=snapshot["summary"], settings=snapshot["settings"], portfolios=snapshot["portfolios"], transactions=snapshot["transactions"], snapshot_time=snapshot["generated_at"][:19].replace("T", " ") + " UTC")


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
    snapshot, markdown, path = generate_report(Database(), save=True)
    return jsonify({"status": "ok", "report_path": str(path), "summary": snapshot["summary"], "markdown": markdown})


@app.get("/api/report")
def api_report():
    snapshot, markdown, path = generate_report(Database(), save=True)
    return jsonify({"snapshot": snapshot, "report_path": str(path), "markdown": markdown})


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


OPERATOR_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ section|title }} // Portfolio NOC</title>
<style>
:root{color-scheme:dark;--bg:#080d14;--panel:#0e1723;--line:#203247;--text:#dce8f4;--muted:#8498ad;--cyan:#43d9ff;--green:#5ee6a8;--amber:#f5c66d;--red:#ff7184}*{box-sizing:border-box}body{font:14px system-ui;background:var(--bg);color:var(--text);margin:0}.shell{max-width:1280px;margin:auto;padding:26px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}.brand{font-weight:800;letter-spacing:.12em}.muted{color:var(--muted)}a{color:var(--cyan);text-decoration:none}.nav{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0}.nav a,.button,button{border:1px solid #2b6277;border-radius:8px;background:#123344;color:#dff8ff;padding:9px 12px;cursor:pointer;font-weight:700}.nav a.active{background:#1b536b}.button.danger,button.danger{background:#321a25;border-color:#6b3040;color:#ffb4bd}.card{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:18px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.span-6{grid-column:span 6}.span-12{grid-column:span 12}.card h2{margin-top:0}.form-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}label{display:grid;gap:5px;color:var(--muted);font-size:12px}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:8px;background:#09121d;color:var(--text);padding:10px}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #1b2a3c;padding:9px 7px;vertical-align:top}.table-wrap{overflow:auto}.status{margin-top:14px;padding:10px;border-left:3px solid var(--cyan);background:#0a2430;white-space:pre-wrap}.status.error{border-color:var(--red);background:#2a151c}.pill{font-size:11px;color:var(--green);border:1px solid #2c7257;border-radius:99px;padding:4px 8px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}@media(max-width:800px){.span-6{grid-column:span 12}.form-grid{grid-template-columns:1fr}}
</style></head><body><div class="shell"><div class="top"><div class="brand">PORTFOLIO // NOC</div><span class="pill">LOCAL / PAPER MODE</span></div>
<nav class="nav">{% for name,label in [('','Portfolio'),('agents','Agents'),('trades','Trades'),('integrations','Integrations'),('strategies','Strategies'),('scheduler','Scheduler'),('audit','Audit / Risk'),('settings','Settings')] %}<a class="{{ 'active' if section == name else '' }}" href="{{ '/' if not name else '/page/' + name }}">{{ label }}</a>{% endfor %}<a href="/">← Dashboard</a></nav>
<div class="card"><h1>{{ section|title }}</h1><p class="muted">{{ description }}</p></div>
{% if section == 'agents' %}
<div class="card"><h2>Local agent control plane</h2><p class="muted">All agent behavior is controlled here. Runs are bounded, durable, paper-first, and never execute shell commands or live broker writes.</p></div><div class="grid">{% for agent in data.agents %}<div class="card span-6"><h2>{{ agent.name }}</h2><p class="muted">{{ agent.description }}</p><p>Mode: <strong>{{ agent.mode }}</strong> · Enabled: <strong>{{ 'yes' if agent.enabled else 'no' }}</strong></p><p>Last run: <strong>{{ agent.last_run_status or 'never' }}</strong>{% if agent.last_run_started_at %} at {{ agent.last_run_started_at }}{% endif %}</p><div class="actions"><button onclick="agentAction('{{ agent.name }}','toggle',{{ 'false' if agent.enabled else 'true' }})">{{ 'Disable' if agent.enabled else 'Enable' }}</button><button onclick="agentAction('{{ agent.name }}','run',false)">Run now</button><button onclick="agentAction('{{ agent.name }}','test',true)">Run test / dry-run</button><button onclick="agentAction('{{ agent.name }}','retry',false)">Retry last run</button><button onclick="agentLogs('{{ agent.name }}')">View logs</button></div></div>{% endfor %}</div><div id="result" class="status">Select an action. Validation and failures are shown here.</div>
{% elif section == 'trades' %}
<div class="grid"><div class="card span-6"><h2>Place paper order</h2><form id="paper-order" class="form-grid"><label>Symbol<input name="symbol" placeholder="INFY" required></label><label>Side<select name="side"><option>buy</option><option>sell</option></select></label><label>Quantity<input name="quantity" type="number" step="0.0001" required></label><label>Quote price<input name="quote_price" type="number" step="0.01" required></label><label>Currency<input name="currency" value="INR"></label><label>Account ID<input name="account_id" value="operator"></label><label>Portfolio ID<input name="portfolio_id" value="Main"></label><label>Idempotency key<input name="idempotency_key" required></label><div class="actions"><button>Submit paper order</button><button type="button" class="danger" onclick="postJson('/api/risk/kill-switch',{state:'on'})">Enable kill switch</button></div></form><div id="result" class="status">Ready.</div></div><div class="card span-6"><h2>Order history</h2><div class="table-wrap"><table><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Status</th><th>Mode</th></tr>{% for o in data.orders %}<tr><td>{{ o.id }}</td><td>{{ o.symbol if o.symbol is defined else o.order_payload }}</td><td>{{ o.side if o.side is defined else '' }}</td><td>{{ o.status }}</td><td>{{ o.mode }}</td></tr>{% else %}<tr><td colspan="5" class="muted">No orders yet.</td></tr>{% endfor %}</table></div></div></div>
{% elif section == 'integrations' %}
<div class="grid">{% for name,status in data.items() %}<div class="card span-6"><h2>{{ name|upper }}</h2><p class="muted">{{ status.status }}</p><p>Configured: <strong>{{ 'yes' if status.configured else 'no' }}</strong> · Live writes: <strong>{{ 'yes' if status.live_writes else 'no' }}</strong> · Read-only: <strong>{{ 'yes' if status.read_only else 'no' }}</strong></p><button onclick="syncIntegration('{{ name }}')">Check / sync {{ name|upper }}</button></div>{% endfor %}</div><div id="result" class="status">No provider call has been made.</div>
{% elif section == 'strategies' %}
<div class="card"><h2>Generate proposal</h2><form id="strategy-form" class="form-grid"><label>Strategy<select name="strategy"><option>kite-short-term/v1</option><option>upstox-long-term/v1</option><option>indmoney-us-read-only/v1</option></select></label><label>Symbol<input name="symbol" value="INFY" required></label><label>Market<input name="market" value="INDIA"></label><label>Currency<input name="currency" value="INR"></label><label>Price<input name="price" type="number" step="0.01" required></label><label>Signal<select name="signal"><option>buy</option><option>sell</option><option>hold</option></select></label><label>Quantity<input name="quantity" value="1"></label><label>Source ID<input name="source_id" value="operator-input"></label><div class="actions"><button>Generate paper proposal</button></div></form><div id="result" class="status">Proposals are suggestions only and do not execute.</div></div>
{% elif section == 'scheduler' %}
<div class="card"><h2>Scheduler controls</h2><p>Enabled: <strong>{{ 'yes' if data.enabled else 'no' }}</strong></p><div class="actions"><button onclick="postJson('/api/scheduler/state',{enabled:'true'})">Enable scheduler</button><button onclick="postJson('/api/scheduler/state',{enabled:'false'})">Disable scheduler</button><button onclick="postJson('/api/scheduler/run',{})">Run due proposals</button></div><div id="result" class="status">Scheduler is paper/proposal-only.</div></div>
{% elif section == 'audit' %}
<div class="card"><h2>Risk controls</h2><p>Kill switch: <strong>{{ data.kill_switch }}</strong></p><div class="actions"><button class="danger" onclick="postJson('/api/risk/kill-switch',{state:'on'})">Turn kill switch ON</button><button onclick="postJson('/api/risk/kill-switch',{state:'off'})">Turn kill switch OFF</button></div><div id="result" class="status">Risk actions are audited.</div></div><div class="card"><h2>Recent audit events</h2><div class="table-wrap"><table><tr><th>Time</th><th>Event</th><th>Entity</th><th>Mode</th></tr>{% for e in data.events %}<tr><td>{{ e.created_at }}</td><td>{{ e.event_type }}</td><td>{{ e.entity_type }} {{ e.entity_id }}</td><td>{{ e.mode }}</td></tr>{% else %}<tr><td colspan="4" class="muted">No audit events.</td></tr>{% endfor %}</table></div></div>
{% elif section == 'settings' %}
<div class="card"><h2>Operator and trading settings</h2><form method="post" action="/settings" class="form-grid"><label>Owner name<input name="owner_name" value="{{ data.owner_name }}"></label><label>Risk profile<select name="risk_profile">{% for option in ['low','balanced','aggressive'] %}<option value="{{ option }}" {% if data.risk_profile == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label><label>Trading mode<select name="trading_mode"><option value="paper" {% if data.trading_mode == 'paper' %}selected{% endif %}>Paper only</option><option value="approval_required" {% if data.trading_mode == 'approval_required' %}selected{% endif %}>Live screens + approval records</option></select></label><label>Max order notional<input name="max_order_notional" value="{{ data.max_order_notional }}"></label><label>Max position weight<input name="max_position_weight" value="{{ data.max_position_weight }}"></label><label>Scheduler enabled<select name="scheduler_enabled"><option value="false" {% if not data.scheduler_enabled %}selected{% endif %}>No</option><option value="true" {% if data.scheduler_enabled %}selected{% endif %}>Yes</option></select></label><label>Default account ID<input name="default_account_id" value="{{ data.default_account_id }}"></label><label>Default portfolio ID<input name="default_portfolio_id" value="{{ data.default_portfolio_id }}"></label><label>Default currency<input name="default_currency" value="{{ data.default_currency }}"></label><label>Monthly contribution<input name="monthly_contribution" value="{{ data.monthly_contribution }}"></label><label>Reminder time<input name="reminder_time" value="{{ data.reminder_time }}"></label><label style="grid-column:1/-1">Notes<textarea name="notes">{{ data.notes }}</textarea></label><button>Save settings</button></form><p class="muted">Trading configuration is stored in the application data volume and is not read from Docker environment variables.</p><div id="result" class="status">Changes are saved through the UI.</div></div>
{% endif %}
</div><script>
async function postJson(url,payload){const box=document.getElementById('result');try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json();box.textContent=JSON.stringify(d,null,2);box.className='status'+(r.ok?'':' error')}catch(e){box.textContent=e;box.className='status error'}}
async function syncIntegration(name){await postJson('/api/integrations/'+name+'/sync',{})}
async function agentAction(name,action,test){const body=action==='toggle'?{enabled:test}:{test:test};const url=action==='toggle'?'/api/agents/'+name:'/api/agents/'+name+'/'+(action==='retry'?'retry':'run');await postJson(url,body)}
async function agentLogs(name){const box=document.getElementById('result');try{const r=await fetch('/api/agents/'+name+'/runs');const d=await r.json();box.textContent=JSON.stringify(d,null,2);box.className='status'+(r.ok?'':' error')}catch(e){box.textContent=e;box.className='status error'}}
document.getElementById('paper-order')?.addEventListener('submit',async e=>{e.preventDefault();const p=Object.fromEntries(new FormData(e.target));await postJson('/api/orders/paper',p)})
document.getElementById('strategy-form')?.addEventListener('submit',async e=>{e.preventDefault();const p=Object.fromEntries(new FormData(e.target));p.as_of=new Date().toISOString();p.now=p.as_of;await postJson('/api/strategies/proposals',p)})
</script></body></html>
"""


@app.get("/page/<section>")
def operator_page(section: str):
    if section not in {"trades", "agents", "integrations", "strategies", "scheduler", "audit", "settings"}:
        return jsonify({"error": "unknown operator page"}), 404
    db = Database(); db.init_db()
    if section == "trades": data = {"orders": _json_rows(db.list_order_intents()), "transactions": _json_rows(db.list_transactions())}
    elif section == "agents": data = {"agents": list_agents(db)}
    elif section == "integrations": data = _integration_status()
    elif section == "scheduler": data = _scheduler_snapshot(db)
    elif section == "audit": data = {"kill_switch": db.get_console_state("global_kill_switch", "off"), "events": _json_rows(db.list_audit_events(limit=100)), "approvals": _json_rows(db.list_live_approvals())}
    elif section == "settings": data = load_settings()
    else: data = {"paper_only": True, "supported": ["kite-short-term/v1", "upstox-long-term/v1", "indmoney-us-read-only/v1"], "note": "Strategies accept caller-supplied facts only and never submit orders."}
    descriptions = {"agents": "Enable, disable, test, run, retry, and inspect durable local agents.", "trades": "Paper orders use the execution gateway; manual transactions remain local ledger records.", "integrations": "Provider connectors are reported honestly from configuration only; no network calls are made.", "strategies": "Generate expiring proposals from supplied facts; proposals do not execute.", "scheduler": "Durable scheduler state and run outcomes.", "audit": "Append-only events, risk posture, kill switch, and live approval records.", "settings": "Local operator preferences and deployment posture."}
    return render_template_string(OPERATOR_PAGE_TEMPLATE, section=section, description=descriptions[section], data=data)


def _integration_status():
    return {name: {"configured": bool(os.environ.get(env)), "status": "configured boundary; no external call" if os.environ.get(env) else "unconfigured MCP connector", "live_writes": False, "read_only": name == "indmoney"} for name, env in (("kite", "KITE_MCP_ENDPOINT"), ("upstox", "UPSTOX_MCP_ENDPOINT"), ("indmoney", "INDMONEY_MCP_ENDPOINT"))}


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
    try: result = run_agent(db, name, run_type="test" if payload.get("test", payload.get("dry_run", False)) else "run")
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
    snapshot, _, path = generate_report(Database(), save=True)
    return jsonify({"snapshot": snapshot, "report_path": str(path)})


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


@app.post("/api/integrations/<name>/sync")
def api_integration_sync(name: str):
    status = _integration_status().get(name)
    if status is None: return jsonify({"error": "unknown integration"}), 404
    return jsonify({"integration": name, "status": "not run", "message": "No provider call made; connector is not configured through this console."}), 409


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


def main(host: str = "127.0.0.1", port: int = 5055, debug: bool = False):
    db = Database(); db.init_db()
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
