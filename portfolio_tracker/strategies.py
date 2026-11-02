"""Deterministic, paper-safe strategy proposal generation.

This module accepts caller-supplied facts only.  It never fetches market data,
reads credentials, or submits an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping

from .trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce, TradeProposal


class StrategyInputError(ValueError):
    """Facts are missing, stale, ambiguous, or routed to the wrong strategy."""


@dataclass(frozen=True, slots=True)
class MarketFact:
    symbol: str
    market: str
    currency: str
    price: Decimal | int | str
    as_of: datetime
    source_id: str
    signal: str = "hold"
    quantity: Decimal | int | str = Decimal("1")
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortfolioFact:
    portfolio_id: str
    account_id: str
    broker_id: str
    market: str
    currency: str
    position_quantity: Mapping[str, Decimal | int | str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NoTradeProposal:
    strategy: str
    rationale: str
    evidence_source_ids: tuple[str, ...]
    evaluated_at: datetime
    expires_at: datetime
    metadata: Mapping[str, str]
    no_trade: bool = True

    def __post_init__(self) -> None:
        _aware(self.evaluated_at, "evaluated_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("expires_at must be after evaluated_at")
        if not self.evidence_source_ids or any(not isinstance(item, str) or not item for item in self.evidence_source_ids):
            raise ValueError("evidence_source_ids must contain non-empty IDs")

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "rationale": self.rationale, "evidence_source_ids": list(self.evidence_source_ids),
                "evaluated_at": self.evaluated_at.isoformat(), "expires_at": self.expires_at.isoformat(),
                "metadata": dict(self.metadata), "no_trade": True}


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """Validated proposal envelope around the existing broker-neutral intent."""
    proposal: TradeProposal
    strategy_version: str
    evidence_source_ids: tuple[str, ...]
    metadata: Mapping[str, str]
    no_trade: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, TradeProposal):
            raise TypeError("proposal must be a TradeProposal")
        if not self.strategy_version or not self.evidence_source_ids:
            raise ValueError("strategy_version and evidence_source_ids are required")
        if self.no_trade:
            raise ValueError("trade proposals cannot be marked no_trade")

    @property
    def intent(self) -> OrderIntent:
        return self.proposal.intent

    @property
    def expires_at(self) -> datetime:
        return self.proposal.expires_at

    def to_dict(self) -> dict:
        result = self.proposal.to_dict()
        result.update({"strategy_version": self.strategy_version, "evidence_source_ids": list(self.evidence_source_ids),
                       "metadata": dict(self.metadata), "no_trade": False})
        return result


ProposalResult = StrategyProposal | NoTradeProposal


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StrategyInputError(f"{name} must be timezone-aware")
    return value


def _validate(fact: MarketFact, portfolio: PortfolioFact, now: datetime, *, broker: str, market: str, currency: str, max_age: timedelta) -> tuple[Decimal, datetime]:
    _aware(now, "now")
    _aware(fact.as_of, "market fact as_of")
    if fact.market.upper() != market or portfolio.market.upper() != market:
        raise StrategyInputError("market is not routed to this strategy")
    if fact.currency.upper() != currency or portfolio.currency.upper() != currency:
        raise StrategyInputError("currency is not routed to this strategy")
    if portfolio.broker_id != broker:
        raise StrategyInputError("broker is not routed to this strategy")
    if now - fact.as_of > max_age or fact.as_of > now + timedelta(seconds=1):
        raise StrategyInputError("market facts are stale or from the future")
    if not fact.symbol or ":" in fact.symbol or " " in fact.symbol:
        raise StrategyInputError("symbol must be unambiguous")
    if not fact.source_id:
        raise StrategyInputError("market source_id is required")
    price = Decimal(str(fact.price))
    quantity = Decimal(str(fact.quantity))
    if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity <= 0:
        raise StrategyInputError("price and quantity must be positive finite values")
    return price, now


def _result(fact: MarketFact, portfolio: PortfolioFact, now: datetime, *, strategy: str, broker: str, ttl: timedelta, side: OrderSide | None, rationale: str) -> ProposalResult:
    price, evaluated = _validate(fact, portfolio, now, broker=broker, market="INDIA", currency="INR", max_age=timedelta(hours=1))
    expires = evaluated + ttl
    evidence = tuple(dict.fromkeys((fact.source_id, *fact.evidence)))
    metadata = {"broker_id": broker, "account_id": portfolio.account_id, "portfolio_id": portfolio.portfolio_id, "mode": "paper", "market": "INDIA", "currency": "INR"}
    if side is None:
        return NoTradeProposal(strategy=strategy, rationale=rationale, evidence_source_ids=evidence, evaluated_at=evaluated, expires_at=expires, metadata=metadata)
    intent = OrderIntent(idempotency_key=f"{strategy}:{portfolio.portfolio_id}:{fact.symbol}:{evaluated.isoformat()}", portfolio_id=portfolio.portfolio_id,
                         account_id=portfolio.account_id, symbol=fact.symbol, side=side, order_type=OrderType.LIMIT,
                         time_in_force=TimeInForce.DAY, quantity=fact.quantity, currency="INR", mode=ExecutionMode.PAPER, limit_price=price)
    proposal = TradeProposal(proposal_id=f"proposal-{intent.idempotency_key}", intent=intent, strategy=strategy, rationale=rationale, expires_at=expires)
    return StrategyProposal(proposal=proposal, strategy_version=strategy, evidence_source_ids=evidence, metadata=metadata)


def propose_kite_short_term(fact: MarketFact, portfolio: PortfolioFact, *, now: datetime, ttl: timedelta = timedelta(minutes=30)) -> ProposalResult:
    """Create a paper-only NSE/BSE short-term proposal routed to Kite."""
    signal = fact.signal.lower()
    side = OrderSide.BUY if signal == "buy" else OrderSide.SELL if signal == "sell" else None
    return _result(fact, portfolio, now, strategy="kite-short-term/v1", broker="kite", ttl=ttl, side=side,
                   rationale=f"Deterministic short-term signal={signal}; paper proposal only." if side else f"No trade: short-term signal={signal}.")


def propose_upstox_long_term(fact: MarketFact, portfolio: PortfolioFact, *, now: datetime, ttl: timedelta = timedelta(days=1)) -> ProposalResult:
    """Create a paper-only Indian long-term proposal routed to Upstox."""
    signal = fact.signal.lower()
    side = OrderSide.BUY if signal == "buy" else OrderSide.SELL if signal == "sell" else None
    return _result(fact, portfolio, now, strategy="upstox-long-term/v1", broker="upstox", ttl=ttl, side=side,
                   rationale=f"Deterministic long-term signal={signal}; paper proposal only." if side else f"No trade: long-term signal={signal}.")


def propose_indmoney_us_tracking(fact: MarketFact, portfolio: PortfolioFact, *, now: datetime, ttl: timedelta = timedelta(days=1)) -> NoTradeProposal:
    """Return an explicit no-trade result for read-only INDmoney US tracking."""
    _aware(now, "now")
    _aware(fact.as_of, "market fact as_of")
    if fact.market.upper() != "US" or portfolio.market.upper() != "US" or fact.currency.upper() != "USD" or portfolio.currency.upper() != "USD":
        raise StrategyInputError("INDmoney strategy accepts only unambiguous US/USD tracking facts")
    if portfolio.broker_id != "indmoney" or now - fact.as_of > timedelta(hours=24):
        raise StrategyInputError("INDmoney facts are stale or wrongly routed")
    if not fact.source_id or not fact.symbol or " " in fact.symbol or ":" in fact.symbol:
        raise StrategyInputError("symbol and source_id are required")
    evidence = tuple(dict.fromkeys((fact.source_id, *fact.evidence)))
    return NoTradeProposal(strategy="indmoney-us-read-only/v1", rationale="Read-only US tracking; execution is disabled.", evidence_source_ids=evidence,
                            evaluated_at=now, expires_at=now + ttl,
                            metadata={"broker_id": "indmoney", "account_id": portfolio.account_id, "portfolio_id": portfolio.portfolio_id, "mode": "read_only", "market": "US", "currency": "USD"})

# Friendly names for callers and plan terminology.
short_term_india_signal = propose_kite_short_term
long_term_india_proposal = propose_upstox_long_term
indmoney_us_tracking = propose_indmoney_us_tracking
