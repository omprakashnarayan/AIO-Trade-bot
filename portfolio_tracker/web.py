from __future__ import annotations

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from .config import load_settings, save_settings
from .db import Database
from .reporting import generate_report

app = Flask(__name__)

action_template = """
<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Portfolio Tracker</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; max-width: 1200px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; }
    input, select, textarea { width: 100%; padding: 8px; margin: 4px 0 12px; box-sizing: border-box; }
    button { padding: 10px 14px; border: 0; border-radius: 8px; cursor: pointer; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align: left; vertical-align: top; }
    code, pre { background: #f7f7f7; padding: 2px 4px; border-radius: 6px; }
    .portfolio { margin-top: 18px; padding-top: 12px; border-top: 1px solid #eee; }
  </style>
</head>
<body>
  <h1>Portfolio Tracker</h1>
  <p>Local tracker for holdings, price snapshots, and daily suggestions.</p>
  <p><a href='{{ url_for("api_report") }}'>JSON report</a> · <a href='{{ url_for("refresh") }}'>Refresh & save report</a></p>

  <div class='grid'>
    <div class='card'>
      <h2>Summary</h2>
      <ul>
        <li>Portfolios tracked: {{ summary.portfolios }}</li>
        <li>Positions tracked: {{ summary.positions }}</li>
      </ul>
    </div>

    <div class='card'>
      <h2>Settings</h2>
      <form method='post' action='{{ url_for("update_settings") }}'>
        <label>Owner name</label>
        <input name='owner_name' value='{{ settings.owner_name }}'>
        <label>Risk profile</label>
        <select name='risk_profile'>
          {% for option in ['low', 'balanced', 'aggressive'] %}
            <option value='{{ option }}' {% if settings.risk_profile == option %}selected{% endif %}>{{ option }}</option>
          {% endfor %}
        </select>
        <label>Monthly contribution</label>
        <input name='monthly_contribution' value='{{ settings.monthly_contribution }}'>
        <label>Reminder time</label>
        <input name='reminder_time' value='{{ settings.reminder_time }}' placeholder='08:00'>
        <label>Notes</label>
        <textarea name='notes' rows='3'>{{ settings.notes }}</textarea>
        <button type='submit'>Save settings</button>
      </form>
    </div>

    <div class='card'>
      <h2>Add / update holding</h2>
      <form method='post' action='{{ url_for("add_holding") }}'>
        <label>Portfolio</label>
        <input name='portfolio' value='Main'>
        <label>Currency</label>
        <input name='currency' value='INR'>
        <label>Symbol</label>
        <input name='symbol' required>
        <label>Label</label>
        <input name='label'>
        <label>Shares</label>
        <input name='shares' type='number' step='0.0001' required>
        <label>Average cost</label>
        <input name='avg_cost' type='number' step='0.01' required>
        <label>Notes</label>
        <input name='notes'>
        <button type='submit'>Save holding</button>
      </form>
    </div>

    <div class='card'>
      <h2>Add transaction</h2>
      <form method='post' action='{{ url_for("add_transaction") }}'>
        <label>Portfolio</label>
        <input name='portfolio' value='Main'>
        <label>Currency</label>
        <input name='currency' value='INR'>
        <label>Symbol</label>
        <input name='symbol' required>
        <label>Side</label>
        <select name='side'><option>buy</option><option>sell</option></select>
        <label>Shares</label>
        <input name='shares' type='number' step='0.0001' required>
        <label>Price</label>
        <input name='price' type='number' step='0.01' required>
        <label>Fees</label>
        <input name='fees' type='number' step='0.01' value='0'>
        <label>Trade date</label>
        <input name='trade_date' placeholder='YYYY-MM-DD'>
        <label>Notes</label>
        <input name='notes'>
        <button type='submit'>Save transaction</button>
      </form>
    </div>
  </div>

  {% for section in portfolios %}
  <div class='card portfolio'>
    <h2>{{ section.portfolio }} <small>({{ section.currency }})</small></h2>
    <ul>
      <li>Total value: {{ money(section.summary.total_value, section.currency) }}</li>
      <li>Cost basis: {{ money(section.summary.total_cost, section.currency) }}</li>
      <li>Gain/loss: {{ money(section.summary.gain, section.currency) }} ({{ pct(section.summary.gain_pct) }})</li>
      <li>Positions: {{ section.summary.positions }}</li>
    </ul>
    <table>
      <thead><tr><th>Name</th><th>Shares</th><th>Avg cost</th><th>Current price</th><th>Value</th><th>P/L</th><th>Weight</th></tr></thead>
      <tbody>
      {% for h in section.holdings %}
        <tr>
          <td>{{ h.label or h.symbol }}</td>
          <td>{{ '%.4f'|format(h.shares) }}</td>
          <td>{{ money(h.avg_cost, section.currency) }}</td>
          <td>{{ money(h.current_price, section.currency) if h.current_price is not none else 'n/a' }}</td>
          <td>{{ money(h.market_value, section.currency) }}</td>
          <td>{{ money(h.gain, section.currency) }} ({{ pct(h.gain_pct) }})</td>
          <td>{{ pct(h.weight * 100) }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>

    <div class='grid' style='margin-top: 12px;'>
      <div class='card'>
        <h3>Long-term ideas</h3>
        <ul>{% for item in section.recommendations.long_term %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
      <div class='card'>
        <h3>Short-term ideas</h3>
        <ul>{% for item in section.recommendations.short_term %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
      <div class='card'>
        <h3>Watchouts</h3>
        <ul>{% for item in section.recommendations.risks %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
    </div>
  </div>
  {% endfor %}

  <div class='card'>
    <h2>Recent transactions</h2>
    <table>
      <thead><tr><th>Date</th><th>Portfolio</th><th>Symbol</th><th>Side</th><th>Shares</th><th>Price</th><th>Currency</th><th>Fees</th><th>Notes</th></tr></thead>
      <tbody>
      {% for t in transactions %}
        <tr>
          <td>{{ t.trade_date }}</td><td>{{ t.portfolio }}</td><td>{{ t.symbol }}</td><td>{{ t.side }}</td><td>{{ t.shares }}</td><td>{{ money(t.price, t.currency) }}</td><td>{{ t.currency }}</td><td>{{ money(t.fees, t.currency) }}</td><td>{{ t.notes }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


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


@app.context_processor
def inject_helpers():
    return {"money": _money, "pct": _pct}


@app.get("/")
def index():
    db = Database()
    snapshot, _, _ = generate_report(db, save=True)
    return render_template_string(
        action_template,
        summary=snapshot["summary"],
        settings=snapshot["settings"],
        portfolios=snapshot["portfolios"],
        transactions=snapshot["transactions"],
    )


@app.post("/settings")
def update_settings():
    settings = load_settings()
    for key in ["owner_name", "risk_profile", "monthly_contribution", "reminder_time", "notes"]:
        settings[key] = request.form.get(key, settings.get(key, ""))
    save_settings(settings)
    return redirect(url_for("index"))


@app.post("/holdings")
def add_holding():
    db = Database()
    db.init_db()
    db.upsert_holding(
        symbol=request.form["symbol"].strip().upper(),
        label=request.form.get("label", "").strip(),
        shares=float(request.form["shares"]),
        avg_cost=float(request.form["avg_cost"]),
        notes=request.form.get("notes", "").strip(),
        portfolio=request.form.get("portfolio", "Main").strip() or "Main",
        currency=request.form.get("currency", "INR").strip().upper() or "INR",
    )
    return redirect(url_for("index"))


@app.post("/transactions")
def add_transaction():
    db = Database()
    db.init_db()
    db.adjust_holding(
        symbol=request.form["symbol"].strip().upper(),
        delta_shares=float(request.form["shares"]),
        trade_price=float(request.form["price"]),
        side=request.form["side"],
        fees=float(request.form.get("fees", 0) or 0),
        trade_date=request.form.get("trade_date") or None,
        notes=request.form.get("notes", "").strip(),
        portfolio=request.form.get("portfolio", "Main").strip() or "Main",
        currency=request.form.get("currency", "INR").strip().upper() or "INR",
    )
    return redirect(url_for("index"))


@app.get("/refresh")
def refresh():
    db = Database()
    snapshot, markdown, path = generate_report(db, save=True)
    return jsonify({"status": "ok", "report_path": str(path), "summary": snapshot["summary"], "markdown": markdown})


@app.get("/api/report")
def api_report():
    db = Database()
    snapshot, markdown, path = generate_report(db, save=True)
    return jsonify({"snapshot": snapshot, "report_path": str(path), "markdown": markdown})


def main(host: str = "127.0.0.1", port: int = 5055, debug: bool = False):
    db = Database()
    db.init_db()
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
