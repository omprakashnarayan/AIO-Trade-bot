"""Honest portfolio analytics: missing or stale inputs remain unavailable."""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timedelta, timezone
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

def _mark(raw: Any, currency: str) -> D | None:
    if isinstance(raw, dict):
        if str(raw.get("currency", "")).upper() != currency: return None
        try:
            stamp=datetime.fromisoformat(str(raw["as_of"]).replace("Z", "+00:00"))
            age=datetime.now(timezone.utc)-stamp
        except (KeyError, ValueError, TypeError, OverflowError): return None
        if stamp.tzinfo is None or not timedelta(0) <= age <= timedelta(days=7): return None
        raw=raw.get("price")
    if raw is None: return None
    try: price=dec(raw,"price")
    except ValueError: return None
    return price if price > 0 else None

def _price_for(raw: Any, symbol: str, currency: str, persisted: dict[str, dict]) -> D | None:
    if raw is None: return None
    if not isinstance(raw, dict) and symbol in persisted:
        latest=persisted[symbol]
        # Legacy scalar callers discard metadata. Reattach the latest recorded
        # quote only when its price matches; never assign it another currency.
        if str(raw) != str(latest["price"]): return None
        raw=latest
    return _mark(raw,currency)

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

def portfolio_analytics(db: Any, *, portfolio: str | None = None, base_currency: str = "INR", prices: dict[Any, Any] | None = None, rates: dict[tuple[str,str], Any] | None = None, stale_symbols: set[str | tuple[str,str]] | None = None, valuation_series: Iterable[Any] | None = None, benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
    base_currency=base_currency.upper(); prices=prices or {}; rates=rates or {}; stale_symbols=stale_symbols or set()
    holdings=db.list_holdings(portfolio); total=D("0"); cost=D("0"); unavailable=[]; by_currency={}; by_portfolio={}; positions=[]; missing_fx=False
    symbol_currencies={}
    for row in holdings: symbol_currencies.setdefault(row["symbol"].upper(),set()).add(row["currency"].upper())
    persisted={}
    for row in db.list_market_quotes(limit=500):
        persisted.setdefault(row["symbol"].upper(),dict(row))
    for row in holdings:
        symbol=row["symbol"].upper(); qty=dec(row["shares"],"shares"); avg=dec(row["avg_cost"],"avg_cost"); currency=row["currency"].upper(); fx=_fx(currency,base_currency,rates)
        raw=prices.get((symbol,currency))
        if raw is None and len(symbol_currencies[symbol]) == 1: raw=prices.get(symbol)
        price=None if symbol in stale_symbols or (symbol,currency) in stale_symbols else _price_for(raw,symbol,currency,persisted)
        value=None if price is None or fx is None else qty*price*fx
        invested=qty*avg*fx if fx is not None else None
        if invested is not None: cost += invested
        else: missing_fx=True
        if value is None: unavailable.append(symbol)
        else: total += value; by_currency[currency]=by_currency.get(currency,D("0"))+value; by_portfolio[row["portfolio"]]=by_portfolio.get(row["portfolio"],D("0"))+value
        positions.append({"symbol":symbol,"quantity":_q(qty),"currency":currency,"price":_q(price) if price is not None else None,"value":_q(value) if value is not None else None,"source":"verified/manual" if price is not None else "unavailable"})
    # Provider scopes are reported separately; they never overwrite manual holdings.
    # Broker snapshots have a different account scope and no trusted live marks.
    # Never divide broker values by the manual-holdings total.
    events=db.list_financial_events(namespace="manual", portfolio=portfolio)
    ledger=calculate(db, namespace="manual", portfolio=portfolio) if events else {"realized_pl":"0","unrealized_pl":"0"}
    realized_available=all(row["currency"].upper() == base_currency and row["fx_rate"] is None for row in events)
    concentration=lambda m: {k:_q(v/total) for k,v in m.items()} if total else {}
    result={"available":not unavailable and not missing_fx,"base_currency":base_currency,"portfolio":portfolio or "all","value":_q(total) if not unavailable and not missing_fx else None,"cost_basis":_q(cost) if not missing_fx else None,"realized_pl":ledger["realized_pl"] if realized_available else None,"unrealized_pl":_q(total-cost) if not unavailable and not missing_fx else None,"unavailable_symbols":sorted(set(unavailable)),"positions":positions,"concentration":{"currency":concentration(by_currency) if not unavailable and not missing_fx else {},"portfolio":concentration(by_portfolio) if not unavailable and not missing_fx else {},"broker":{}},"drawdown":max_drawdown(valuation_series or [])}
    if benchmark: result["benchmark_comparison"]=benchmark_comparison(**benchmark)
    else: result["benchmark_comparison"]={"available":False,"reason":"benchmark snapshots unavailable"}
    return result
