"""Honest portfolio analytics: missing or stale inputs remain unavailable."""
from __future__ import annotations
from decimal import Decimal
from typing import Any, Iterable
from .ledger import calculate, dec

D = Decimal

def _q(value: D) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
def _fx(currency: str, base: str, rates: dict[tuple[str,str], Any]) -> D | None:
    if currency == base: return D("1")
    value = rates.get((currency, base))
    return None if value is None else dec(value, "fx_rate")

def max_drawdown(values: Iterable[Any]) -> dict[str, Any]:
    peak = None; worst = D("0"); peak_value = trough_value = None
    for raw in values:
        value = dec(raw, "portfolio_value")
        if value < 0: raise ValueError("portfolio values must be nonnegative")
        if peak is None or value > peak: peak = value
        if peak and peak > 0 and (peak-value)/peak > worst: worst=(peak-value)/peak; peak_value=peak; trough_value=value
    return {"available": peak is not None, "drawdown": _q(worst), "peak": _q(peak_value) if peak_value is not None else None, "trough": _q(trough_value) if trough_value is not None else None}

def benchmark_comparison(portfolio_start: Any, portfolio_end: Any, benchmark_start: Any, benchmark_end: Any, *, as_of: str, benchmark_as_of: str) -> dict[str, Any]:
    ps, pe, bs, be = map(lambda x: dec(x, "benchmark value"), (portfolio_start, portfolio_end, benchmark_start, benchmark_end))
    if min(ps, bs) <= 0: raise ValueError("comparison bases must be positive")
    portfolio_return=(pe/ps)-1; benchmark_return=(be/bs)-1
    return {"portfolio_return":_q(portfolio_return),"benchmark_return":_q(benchmark_return),"excess_return":_q(portfolio_return-benchmark_return),"as_of":as_of,"benchmark_as_of":benchmark_as_of}

def portfolio_analytics(db: Any, *, portfolio: str | None = None, base_currency: str = "INR", prices: dict[str, Any] | None = None, rates: dict[tuple[str,str], Any] | None = None, stale_symbols: set[str] | None = None, valuation_series: Iterable[Any] | None = None, benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
    base_currency=base_currency.upper(); prices=prices or {}; rates=rates or {}; stale_symbols=stale_symbols or set()
    holdings=db.list_holdings(portfolio); total=D("0"); cost=D("0"); unavailable=[]; by_currency={}; by_broker={}; by_portfolio={}; positions=[]
    for row in holdings:
        symbol=row["symbol"].upper(); qty=dec(row["shares"],"shares"); avg=dec(row["avg_cost"],"avg_cost"); currency=row["currency"].upper(); fx=_fx(currency,base_currency,rates)
        price=None if symbol not in prices or symbol in stale_symbols else dec(prices[symbol],"price")
        value=None if price is None or fx is None else qty*price*fx
        invested=qty*avg*fx if fx is not None else None
        if invested is not None: cost += invested
        if value is None: unavailable.append(symbol)
        else: total += value; by_currency[currency]=by_currency.get(currency,D("0"))+value; by_portfolio[row["portfolio"]]=by_portfolio.get(row["portfolio"],D("0"))+value
        positions.append({"symbol":symbol,"quantity":_q(qty),"currency":currency,"price":_q(price) if price is not None else None,"value":_q(value) if value is not None else None,"source":"verified/manual" if price is not None else "unavailable"})
    # Provider scopes are reported separately; they never overwrite manual holdings.
    try:
        with db.connect() as conn: provider_rows = conn.execute("SELECT broker_id,symbol,quantity,currency FROM provider_holdings WHERE broker_id IN ('kite','upstox','paper')").fetchall()
    except Exception:
        provider_rows = []
    for row in provider_rows:
        broker_id=row["broker_id"]; symbol=row["symbol"].upper(); price=prices.get(symbol); fx=_fx(row["currency"].upper(), base_currency, rates)
        if price is not None and fx is not None: by_broker[broker_id]=by_broker.get(broker_id,D("0"))+dec(row["quantity"],"quantity")*dec(price,"price")*fx
    ledger=calculate(db, namespace="manual", portfolio=portfolio, market_prices=prices) if db.list_financial_events(namespace="manual", portfolio=portfolio) else {"realized_pl":"0","unrealized_pl":"0"}
    concentration=lambda m: {k:_q(v/total) for k,v in m.items()} if total else {}
    result={"available":not unavailable,"base_currency":base_currency,"portfolio":portfolio or "all","value":_q(total) if not unavailable else None,"cost_basis":_q(cost),"realized_pl":ledger["realized_pl"],"unrealized_pl":_q(total-cost) if not unavailable else None,"unavailable_symbols":sorted(set(unavailable)),"positions":positions,"concentration":{"currency":concentration(by_currency),"portfolio":concentration(by_portfolio),"broker":concentration(by_broker)},"drawdown":max_drawdown(valuation_series or [])}
    if benchmark: result["benchmark_comparison"]=benchmark_comparison(**benchmark)
    else: result["benchmark_comparison"]={"available":False,"reason":"benchmark snapshots unavailable"}
    return result
