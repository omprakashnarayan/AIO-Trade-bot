from __future__ import annotations

from collections import defaultdict


def build_recommendations(summary: dict, holdings: list[dict], settings: dict) -> dict:
    risk = (settings.get("risk_profile") or "balanced").lower()
    total_value = summary.get("total_value", 0.0) or 0.0
    total_cost = summary.get("total_cost", 0.0) or 0.0

    long_term: list[str] = []
    short_term: list[str] = []
    risks: list[str] = []

    if not holdings:
        long_term.append("No holdings tracked yet. A simple diversified core like a broad-market ETF is often a cleaner long-term starting point than picking many single stocks.")
        short_term.append("Add your current holdings or watchlist so daily updates can be based on real positions.")
        return {"long_term": long_term, "short_term": short_term, "risks": risks}

    biggest = max(holdings, key=lambda h: h.get("weight", 0.0))
    if biggest.get("weight", 0.0) >= 0.35:
        risks.append(f"{biggest['symbol']} is {biggest['weight']*100:.1f}% of portfolio value — concentration is high.")
        long_term.append(f"Consider trimming concentration in {biggest['symbol']} if you want a steadier long-term profile.")

    stock_count = len(holdings)
    if stock_count <= 3 and total_value > 0:
        risks.append("Portfolio is small and concentrated; a broad index ETF can help reduce single-name risk.")

    daily_movers = sorted(holdings, key=lambda h: abs(h.get("day_change_pct") or 0.0), reverse=True)
    for item in daily_movers[:3]:
        change = item.get("day_change_pct")
        if change is None:
            continue
        if change <= -5:
            short_term.append(f"{item['symbol']} is down {change:.1f}% today. Check whether the move is news-driven or just market noise before reacting.")
        elif change >= 5:
            short_term.append(f"{item['symbol']} is up {change:.1f}% today. If you already have a target size, avoid chasing the move emotionally.")

    if risk == "low":
        long_term.append("For low risk, emphasize diversified funds, smaller position sizes, and fewer single-stock bets.")
    elif risk == "aggressive":
        long_term.append("For aggressive risk, higher volatility can be acceptable, but keep a written plan for entry size and exit rules.")
    else:
        long_term.append("For a balanced profile, a core-satellite approach usually works well: diversified core plus a few conviction picks.")

    if total_cost > 0 and total_value > 0:
        return_pct = (total_value - total_cost) / total_cost
        if return_pct >= 0.20:
            short_term.append("Your portfolio is up a lot relative to cost basis. Re-check whether any position has grown beyond your intended size.")
        elif return_pct <= -0.15:
            short_term.append("The portfolio is below cost basis. Focus on whether the original thesis is still valid before averaging down.")

    if not short_term:
        short_term.append("No major short-term action stands out today. Continue tracking price changes and keep new buys aligned with your plan.")

    return {"long_term": long_term, "short_term": short_term, "risks": risks}
