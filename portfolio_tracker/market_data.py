"""Provider-neutral, provenance-preserving market data services."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, Sequence


def _dec(value: Any, field: str) -> Decimal:
    try: result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError): raise ValueError(f"{field} must be a decimal") from None
    if not result.is_finite(): raise ValueError(f"{field} must be finite")
    return result

def _ts(value: Any) -> str:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError("timestamp must be timezone-aware")
    return dt.isoformat()

def _now() -> str: return datetime.now(timezone.utc).isoformat()

@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str; price: Decimal; currency: str; as_of: str; source: str
    market_status: str = "unknown"; exchange: str = ""; captured_at: str = ""
    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "price", _dec(self.price, "price"))
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "as_of", _ts(self.as_of)); object.__setattr__(self, "captured_at", _ts(self.captured_at or _now()))
        if not self.source.strip(): raise ValueError("source is required")

@dataclass(frozen=True, slots=True)
class HistoricalBar:
    symbol: str; interval: str; open: Decimal; high: Decimal; low: Decimal; close: Decimal
    as_of: str; source: str; currency: str = "INR"; volume: Decimal | None = None
    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper()); object.__setattr__(self, "currency", self.currency.upper()); object.__setattr__(self, "as_of", _ts(self.as_of))
        for name in ("open","high","low","close"): object.__setattr__(self, name, _dec(getattr(self,name), name))
        if self.volume is not None: object.__setattr__(self, "volume", _dec(self.volume, "volume"))

@dataclass(frozen=True, slots=True)
class FXRate:
    base_currency: str; quote_currency: str; rate: Decimal; as_of: str; source: str; captured_at: str = ""
    def __post_init__(self):
        object.__setattr__(self, "base_currency", self.base_currency.upper()); object.__setattr__(self, "quote_currency", self.quote_currency.upper()); object.__setattr__(self, "rate", _dec(self.rate,"rate")); object.__setattr__(self, "as_of", _ts(self.as_of)); object.__setattr__(self, "captured_at", _ts(self.captured_at or _now()))
        if self.rate <= 0: raise ValueError("rate must be positive")

@dataclass(frozen=True, slots=True)
class BenchmarkSnapshot:
    benchmark: str; value: Decimal; as_of: str; source: str; currency: str = "INR"; captured_at: str = ""
    def __post_init__(self):
        object.__setattr__(self,"benchmark",self.benchmark.strip()); object.__setattr__(self,"value",_dec(self.value,"value")); object.__setattr__(self,"as_of",_ts(self.as_of)); object.__setattr__(self,"captured_at",_ts(self.captured_at or _now())); object.__setattr__(self,"currency",self.currency.upper())

@dataclass(frozen=True, slots=True)
class QuoteStatus:
    quote: Quote | None; stale: bool; age_seconds: int | None; available: bool; reason: str

class MarketTransport(Protocol):
    def quotes(self, symbols: Sequence[str]) -> Sequence[Quote]: ...
    def history(self, symbol: str, interval: str = "1d") -> Sequence[HistoricalBar]: ...
    def fx(self, base_currency: str, quote_currency: str) -> FXRate: ...

class ManualMarketTransport:
    def __init__(self): self._quotes: dict[str, Quote] = {}
    def set_quote(self, symbol: str, price: Any, *, currency="INR", as_of: Any = None, market_status="manual") -> Quote:
        quote = Quote(symbol, _dec(price,"price"), currency, as_of or _now(), "manual", market_status=market_status); self._quotes[quote.symbol] = quote; return quote
    def quotes(self, symbols): return [self._quotes[s.upper()] for s in symbols if s.upper() in self._quotes]
    def history(self, symbol, interval="1d"): return []
    def fx(self, base_currency, quote_currency): raise ValueError("manual FX rate unavailable")

class AdapterMarketTransport:
    """Read-only bridge for configured Kite/Upstox adapters; no writes are exposed."""
    def __init__(self, adapter: Any, source: str): self.adapter, self.source = adapter, source
    def quotes(self, symbols):
        return [Quote(q.symbol, q.price, q.currency, q.as_of, self.source, market_status="open", exchange=q.exchange) for s in symbols for q in [self.adapter.quote(s)]]
    def history(self, symbol, interval="1d"): raise ValueError("historical transport is not configured")
    def fx(self, base_currency, quote_currency): raise ValueError("FX transport is not configured")

class MarketDataService:
    def __init__(self, db: Any, transports: dict[str, MarketTransport] | None = None, *, stale_after_seconds: int = 900):
        self.db, self.transports, self.stale_after_seconds = db, transports or {}, stale_after_seconds
    def ingest_quotes(self, source: str, symbols: Sequence[str]) -> list[Quote]:
        if source not in self.transports: return []
        quotes = list(self.transports[source].quotes(symbols))
        for quote in quotes:
            if quote.source != source: quote = Quote(quote.symbol, quote.price, quote.currency, quote.as_of, source, quote.market_status, quote.exchange, quote.captured_at)
            self.db.save_market_quote(quote)
        return quotes
    def manual_snapshot(self, symbol: str, price: Any, *, currency="INR", as_of: Any = None, market_status="manual") -> Quote:
        quote = Quote(symbol, price, currency, as_of or _now(), "manual", market_status=market_status); self.db.save_market_quote(quote); return quote
    def status(self, symbols: Sequence[str]) -> dict[str, QuoteStatus]:
        now = datetime.now(timezone.utc); result = {}
        for symbol in symbols:
            rows = self.db.list_market_quotes(symbol=symbol, limit=20); row = rows[0] if rows else None
            if not row: result[symbol.upper()] = QuoteStatus(None, True, None, False, "no verified quote available"); continue
            quote = Quote(row["symbol"], row["price"], row["currency"], row["as_of"], row["source"], row["market_status"], row["exchange"], row["captured_at"])
            age = max(0, int((now - datetime.fromisoformat(quote.as_of)).total_seconds())); stale = age > self.stale_after_seconds
            result[symbol.upper()] = QuoteStatus(quote, stale, age, not stale, "stale quote" if stale else "fresh")
        return result
    def health(self) -> dict[str, Any]:
        rows = self.db.list_market_quotes(limit=1); return {"configured_sources": sorted(self.transports), "last_update": rows[0]["captured_at"] if rows else None, "available": bool(rows)}
