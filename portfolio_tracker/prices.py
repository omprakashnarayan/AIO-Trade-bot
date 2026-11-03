from __future__ import annotations

from datetime import datetime, timezone

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency until installed
    yf = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_price(symbol: str) -> tuple[float | None, str, str | None]:
    """Return (price, source, error)."""
    symbol = symbol.upper().strip()
    if not symbol:
        return None, "none", "empty symbol"

    if yf is None:
        return None, "none", "yfinance not installed"

    try:
        ticker = yf.Ticker(symbol)
        price = None
        source = "yfinance"

        try:
            info = getattr(ticker, "fast_info", {}) or {}
            price = info.get("lastPrice") or info.get("last_price")
        except Exception:
            pass

        if price is None:
            hist = ticker.history(period="7d", auto_adjust=False, actions=False)
            if not hist.empty:
                price = float(hist["Close"].dropna().iloc[-1])

        if price is None:
            return None, source, "no recent market price returned"
        return float(price), source, None
    except Exception as exc:
        return None, "yfinance", str(exc)


def fetch_prices(symbols: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for symbol in symbols:
        price, source, error = fetch_price(symbol)
        results[symbol.upper()] = {
            "price": price,
            "source": source,
            "error": error,
            "as_of": _iso_now(),
        }
    return results
