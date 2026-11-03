from __future__ import annotations

import math
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from .config import load_settings, save_settings
from .db import Database
from .reporting import generate_report

app = Flask(__name__)


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
  <header class="topbar"><div class="brand"><div class="mark">⌁</div><div>PORTFOLIO <span class="muted">// NOC</span></div></div><div class="top-actions"><span class="pill live">● LOCAL / PAPER MODE</span><a class="button" href="{{ url_for('refresh') }}">↻ Refresh snapshot</a><a href="{{ url_for('api_report') }}">JSON report ↗</a></div></header>
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

_SETTINGS_KEYS = ("owner_name", "risk_profile", "monthly_contribution", "reminder_time", "notes")


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
    return request.get_json(silent=True) or {}


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
    settings = load_settings()
    for key in _SETTINGS_KEYS:
        settings[key] = request.form.get(key, settings.get(key, ""))
    save_settings(settings)
    return redirect(url_for("index"))


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
    settings = load_settings()
    for key in _SETTINGS_KEYS:
        if key in payload:
            settings[key] = str(payload[key])
    if settings.get("risk_profile") not in {"low", "balanced", "aggressive"}:
        return jsonify({"error": "risk_profile must be low, balanced, or aggressive"}), 400
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


def main(host: str = "127.0.0.1", port: int = 5055, debug: bool = False):
    db = Database(); db.init_db()
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
