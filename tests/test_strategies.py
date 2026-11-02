from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_tracker.scheduler import ProposalScheduler, ScheduleTrigger
from portfolio_tracker.strategies import (
    MarketFact,
    NoTradeProposal,
    PortfolioFact,
    StrategyInputError,
    StrategyProposal,
    propose_indmoney_us_tracking,
    propose_kite_short_term,
    propose_upstox_long_term,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def india_fact(signal="buy", *, as_of=NOW, symbol="INFY"):
    return MarketFact(symbol, "INDIA", "INR", Decimal("1500"), as_of, "quote-1", signal, Decimal("2"), ("news-1",))


def india_portfolio(broker="kite"):
    return PortfolioFact("india", "acct-1", broker, "INDIA", "INR", {"INFY": Decimal("5")})


def test_kite_produces_valid_paper_proposal_with_routing_and_evidence():
    result = propose_kite_short_term(india_fact(), india_portfolio(), now=NOW)
    assert isinstance(result, StrategyProposal)
    assert result.intent.mode.value == "paper"
    assert result.intent.account_id == "acct-1"
    assert result.intent.portfolio_id == "india"
    assert result.metadata["broker_id"] == "kite"
    assert result.strategy_version == "kite-short-term/v1"
    assert result.evidence_source_ids == ("quote-1", "news-1")


def test_hold_signal_is_an_explicit_no_trade():
    result = propose_upstox_long_term(india_fact("hold"), india_portfolio("upstox"), now=NOW)
    assert isinstance(result, NoTradeProposal)
    assert result.no_trade is True
    assert result.metadata["mode"] == "paper"


def test_stale_data_and_wrong_routing_are_rejected():
    with pytest.raises(StrategyInputError, match="stale"):
        propose_kite_short_term(india_fact(as_of=NOW - timedelta(hours=2)), india_portfolio(), now=NOW)
    with pytest.raises(StrategyInputError, match="broker"):
        propose_kite_short_term(india_fact(), india_portfolio("upstox"), now=NOW)
    with pytest.raises(StrategyInputError, match="currency"):
        propose_kite_short_term(MarketFact("INFY", "INDIA", "USD", 1, NOW, "q", "buy"), india_portfolio(), now=NOW)


def test_indmoney_is_read_only_and_never_trade():
    fact = MarketFact("AAPL", "US", "USD", 200, NOW, "us-quote", "buy")
    portfolio = PortfolioFact("us", "ind-acct", "indmoney", "US", "USD")
    result = propose_indmoney_us_tracking(fact, portfolio, now=NOW)
    assert isinstance(result, NoTradeProposal)
    assert result.metadata["mode"] == "read_only"
    assert result.metadata["broker_id"] == "indmoney"


def test_ambiguous_symbols_rejected():
    with pytest.raises(StrategyInputError, match="unambiguous"):
        propose_kite_short_term(india_fact(symbol="NSE:INFY"), india_portfolio(), now=NOW)


def test_scheduler_is_timezone_aware_deduplicating_and_paper_only():
    seen = []

    def factory(*, now):
        seen.append(now)
        return propose_kite_short_term(india_fact(as_of=now.astimezone(UTC)), india_portfolio(), now=now)

    scheduler = ProposalScheduler()
    start = datetime(2026, 11, 2, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    scheduler.register(ScheduleTrigger("kite", "Asia/Kolkata", timedelta(hours=1), start, max_missed_runs=3), factory)
    first = scheduler.run_due(datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    duplicate = scheduler.run_due(datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    missed = scheduler.run_due(datetime(2026, 11, 2, 7, 30, tzinfo=UTC))
    assert len(first) == 1
    assert not duplicate
    assert len(missed) == 3  # coalesced to the configured maximum missed runs
    assert all(item.result.intent.mode.value == "paper" for item in (*first, *missed))
    assert all(item.scheduled_at.tzinfo is not None for item in (*first, *missed))
    assert seen[0].hour == 10  # 10:00 IST start, represented at 04:30 UTC


def test_scheduler_rejects_naive_time_and_duplicate_registration():
    scheduler = ProposalScheduler()
    trigger = ScheduleTrigger("x", "UTC", timedelta(hours=1), NOW)
    scheduler.register(trigger, lambda **_: propose_kite_short_term(india_fact(), india_portfolio(), now=NOW))
    with pytest.raises(Exception):
        scheduler.run_due(datetime(2026, 11, 2, 10, 0))
    with pytest.raises(ValueError, match="duplicate"):
        scheduler.register(trigger, lambda **_: propose_kite_short_term(india_fact(), india_portfolio(), now=NOW))
