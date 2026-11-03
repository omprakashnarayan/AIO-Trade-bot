from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .config import REPORTS_DIR, load_settings
from .db import Database
from .prices import fetch_prices
from .recommendations import build_recommendations


def _fmt_money(value: float, currency: str = "$") -> str:
    symbol = "$" if currency == "USD" else "₹" if currency == "INR" else currency + " "
    return f"{symbol}{value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def portfolio_snapshot(db: Database) -> dict:
    holdings_rows = db.list_holdings()
    settings = load_settings()
    if not holdings_rows:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "settings": settings,
            "summary": {"portfolios": 0, "positions": 0},
            "portfolios": [],
            "transactions": [dict(r) for r in db.list_transactions(limit=25)],
        }

    by_portfolio: dict[str, list[dict]] = defaultdict(list)
    currency_by_portfolio: dict[str, str] = {}
    for row in holdings_rows:
        by_portfolio[row["portfolio"]].append(dict(row))
        currency_by_portfolio[row["portfolio"]] = row["currency"]

    portfolio_sections: list[dict] = []
    for portfolio_name, rows in sorted(by_portfolio.items()):
        symbols = [row["symbol"] for row in rows]
        live_prices = fetch_prices(symbols) if symbols else {}
        holdings: list[dict] = []
        total_value = 0.0
        total_cost = 0.0
        currency = currency_by_portfolio.get(portfolio_name, rows[0].get("currency", "INR"))

        for row in rows:
            symbol = row["symbol"]
            shares = float(row["shares"])
            avg_cost = float(row["avg_cost"])
            cached = db.get_price_cache(portfolio_name, symbol)
            price_info = live_prices.get(symbol, {})
            current_price = price_info.get("price")
            source = price_info.get("source") or (cached["source"] if cached else "cache")
            if current_price is None and cached:
                current_price = float(cached["price"])
                source = f"cache:{cached['source']}"
            if current_price is not None:
                db.set_price_cache(
                    portfolio_name,
                    symbol,
                    current_price,
                    price_info.get("as_of") or datetime.now(timezone.utc).isoformat(),
                    source,
                )
            market_value = (current_price or 0.0) * shares
            cost_basis = avg_cost * shares
            gain = market_value - cost_basis
            holdings.append(
                {
                    "portfolio": portfolio_name,
                    "symbol": symbol,
                    "label": row["label"],
                    "currency": currency,
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "current_price": current_price,
                    "source": source,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "gain": gain,
                    "gain_pct": (gain / cost_basis * 100.0) if cost_basis else 0.0,
                    "day_change_pct": None if current_price is None else _infer_day_change_pct(symbol, current_price, cached, live_prices),
                }
            )
            total_value += market_value
            total_cost += cost_basis

        for item in holdings:
            item["weight"] = (item["market_value"] / total_value) if total_value else 0.0

        summary = {
            "portfolio": portfolio_name,
            "currency": currency,
            "total_value": total_value,
            "total_cost": total_cost,
            "gain": total_value - total_cost,
            "gain_pct": ((total_value - total_cost) / total_cost * 100.0) if total_cost else 0.0,
            "positions": len(holdings),
        }
        portfolio_sections.append(
            {
                "portfolio": portfolio_name,
                "currency": currency,
                "summary": summary,
                "holdings": holdings,
                "recommendations": build_recommendations(summary, holdings, settings),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "summary": {
            "portfolios": len(portfolio_sections),
            "positions": sum(section["summary"]["positions"] for section in portfolio_sections),
        },
        "portfolios": portfolio_sections,
        "transactions": [dict(r) for r in db.list_transactions(limit=25)],
    }


def _infer_day_change_pct(symbol: str, current_price: float, cached, live_prices: dict) -> float | None:
    # Best effort only: compare to cached price if it is from a different run.
    if cached and float(cached["price"]) > 0:
        previous = float(cached["price"])
        if abs(previous - current_price) < 1e-9:
            return 0.0
        return (current_price - previous) / previous * 100.0
    return None


def render_markdown_report(snapshot: dict) -> str:
    lines: list[str] = []
    lines.append("# Daily Portfolio Update")
    lines.append(f"_Generated: {snapshot['generated_at']}_")
    lines.append("")
    lines.append("## Summary")
    summary = snapshot["summary"]
    lines.append(f"- Portfolios tracked: {summary['portfolios']}")
    lines.append(f"- Positions tracked: {summary['positions']}")
    lines.append("")

    if not snapshot["portfolios"]:
        lines.append("- No holdings saved yet.")
        return "\n".join(lines).rstrip() + "\n"

    for section in snapshot["portfolios"]:
        currency = section["currency"]
        summary = section["summary"]
        lines.append(f"## {section['portfolio']}")
        lines.append(f"_Currency: {currency}_")
        lines.append(f"- Total value: {_fmt_money(summary['total_value'], currency)}")
        lines.append(f"- Cost basis: {_fmt_money(summary['total_cost'], currency)}")
        lines.append(f"- Gain/loss: {_fmt_money(summary['gain'], currency)} ({_fmt_pct(summary['gain_pct'])})")
        lines.append(f"- Positions: {summary['positions']}")
        lines.append("")
        lines.append("### Holdings")
        for item in section["holdings"]:
            cp = "n/a" if item["current_price"] is None else _fmt_money(item["current_price"], currency)
            display_name = f"{item['label']} ({item['symbol']})" if item.get("label") else item["symbol"]
            lines.append(
                f"- {display_name}: {item['shares']:.4f} shares | price {cp} | value {_fmt_money(item['market_value'], currency)} | weight {_fmt_pct(item['weight']*100)} | P/L {_fmt_money(item['gain'], currency)} ({_fmt_pct(item['gain_pct'])})"
            )
        lines.append("")
        lines.append("### Long-term ideas")
        for item in section["recommendations"]["long_term"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("### Short-term ideas")
        for item in section["recommendations"]["short_term"]:
            lines.append(f"- {item}")
        if section["recommendations"].get("risks"):
            lines.append("")
            lines.append("### Watchouts")
            for item in section["recommendations"]["risks"]:
                lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_report(markdown: str, stamp: str | None = None) -> str | None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).date().isoformat()
    path = REPORTS_DIR / f"{stamp}.md"
    path.write_text(markdown)
    (REPORTS_DIR / "latest.md").write_text(markdown)
    return str(path)


def generate_report(db: Database | None = None, save: bool = True) -> tuple[dict, str, str | None]:
    db = db or Database()
    db.init_db()
    snapshot = portfolio_snapshot(db)
    markdown = render_markdown_report(snapshot)
    path = save_report(markdown, stamp=snapshot["generated_at"][:10]) if save else None
    return snapshot, markdown, path
