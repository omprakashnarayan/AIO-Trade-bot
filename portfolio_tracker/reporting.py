from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite

from .config import REPORTS_DIR, load_settings
from .db import Database
from .market_data import MarketDataService
from .prices import fetch_prices
from .recommendations import build_recommendations


def _fmt_money(value: float, currency: str = "$") -> str:
    symbol = "$" if currency == "USD" else "₹" if currency == "INR" else currency + " "
    return f"{symbol}{value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _verified_quote(info: dict, currency: str, max_age_seconds: int = 900) -> float | None:
    """Only a positive, dated quote explicitly denominated in the holding currency is a mark."""
    if not isinstance(info, dict) or str(info.get("currency", "")).upper() != currency:
        return None
    try:
        price = float(info["price"])
        as_of = datetime.fromisoformat(str(info["as_of"]).replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - as_of
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if as_of.tzinfo is None or not isfinite(price) or price <= 0 or not timedelta(0) <= age <= timedelta(seconds=max_age_seconds):
        return None
    return price


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
    saved_quotes = {}
    for quote in db.list_market_quotes(limit=500):
        saved_quotes.setdefault((quote["symbol"].upper(), quote["currency"].upper()), dict(quote))
    max_age_seconds = MarketDataService(db).stale_after_seconds
    for row in holdings_rows:
        by_portfolio[row["portfolio"]].append(dict(row))

    portfolio_sections: list[dict] = []
    for portfolio_name, rows in sorted(by_portfolio.items()):
        symbols = [row["symbol"] for row in rows]
        live_prices = fetch_prices(symbols) if symbols else {}
        holdings: list[dict] = []
        total_value = 0.0
        total_cost = 0.0
        currencies = {row["currency"].upper() for row in rows}
        currency = next(iter(currencies)) if len(currencies) == 1 else "MIXED"

        for row in rows:
            symbol = row["symbol"]
            shares = float(row["shares"])
            avg_cost = float(row["avg_cost"])
            cached = db.get_price_cache(portfolio_name, symbol)
            price_info = live_prices.get(symbol, {})
            current_price = _verified_quote(price_info, row["currency"].upper(), max_age_seconds)
            if current_price is None:
                price_info = saved_quotes.get((symbol.upper(), row["currency"].upper()), {})
                current_price = _verified_quote(price_info, row["currency"].upper(), max_age_seconds)
            source = (price_info.get("source") or "verified") if current_price is not None else "unavailable"
            # Legacy cache records have no currency: they cannot establish a safe mark.
            if current_price is not None:
                db.set_price_cache(
                    portfolio_name,
                    symbol,
                    current_price,
                    price_info.get("as_of") or datetime.now(timezone.utc).isoformat(),
                    source,
                )
            market_value = current_price * shares if current_price is not None else None
            cost_basis = avg_cost * shares
            gain = market_value - cost_basis if market_value is not None else None
            holdings.append(
                {
                    "portfolio": portfolio_name,
                    "symbol": symbol,
                    "label": row["label"],
                    "currency": row["currency"].upper(),
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "current_price": current_price,
                    "source": source,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "gain": gain,
                    "gain_pct": (gain / cost_basis * 100.0) if gain is not None and cost_basis else (0.0 if gain is not None else None),
                    "day_change_pct": None,  # cache has no currency metadata for safe comparison
                }
            )
            if market_value is not None:
                total_value += market_value
            total_cost += cost_basis

        complete = len(currencies) == 1 and all(item["market_value"] is not None for item in holdings)
        for item in holdings:
            item["weight"] = (item["market_value"] / total_value) if complete and total_value else None

        summary = {
            "portfolio": portfolio_name,
            "currency": currency,
            "total_value": total_value if complete else None,
            "total_cost": total_cost if len(currencies) == 1 else None,
            "gain": total_value - total_cost if complete else None,
            "gain_pct": ((total_value - total_cost) / total_cost * 100.0) if total_cost and complete else (0.0 if complete else None),
            "positions": len(holdings),
        }
        portfolio_sections.append(
            {
                "portfolio": portfolio_name,
                "currency": currency,
                "summary": summary,
                "holdings": holdings,
                "recommendations": build_recommendations(summary, [{**item, "weight": item["weight"] or 0.0} for item in holdings], settings),
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
        money = lambda value, unit=currency: _fmt_money(value, unit) if value is not None else "n/a"
        pct = lambda value: _fmt_pct(value) if value is not None else "n/a"
        lines.append(f"- Total value: {money(summary['total_value'])}")
        lines.append(f"- Cost basis: {money(summary['total_cost'])}")
        lines.append(f"- Gain/loss: {money(summary['gain'])} ({pct(summary['gain_pct'])})")
        lines.append(f"- Positions: {summary['positions']}")
        lines.append("")
        lines.append("### Holdings")
        for item in section["holdings"]:
            unit = item["currency"]
            cp = money(item["current_price"], unit)
            display_name = f"{item['label']} ({item['symbol']})" if item.get("label") else item["symbol"]
            lines.append(
                f"- {display_name}: {item['shares']:.4f} shares | price {cp} | value {money(item['market_value'], unit)} | weight {pct(item['weight']*100 if item['weight'] is not None else None)} | P/L {money(item['gain'], unit)} ({pct(item['gain_pct'])})"
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
