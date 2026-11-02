from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_tracker.db import Database
from portfolio_tracker.scheduler import ProposalScheduler, ScheduleTrigger, SchedulerError
from portfolio_tracker.strategies import MarketFact, PortfolioFact, StrategyInputError, propose_kite_short_term

UTC = timezone.utc


def factory(**kwargs):
    now = kwargs["now"]
    return propose_kite_short_term(MarketFact("INFY", "INDIA", "INR", 1, now, "quote", "hold"), PortfolioFact("p", "a", "kite", "INDIA", "INR"), now=now)


def test_scheduler_zero_missed_runs_is_durable_skip(tmp_path):
    db = Database(tmp_path / "scheduler.sqlite3")
    trigger = ScheduleTrigger("t", "UTC", timedelta(hours=1), datetime(2026, 11, 2, 10, tzinfo=UTC), max_missed_runs=0)
    first = ProposalScheduler(db)
    first.register(trigger, factory)
    assert first.run_due(datetime(2026, 11, 2, 13, tzinfo=UTC)) == ()
    second = ProposalScheduler(db)
    second.register(trigger, factory)
    assert second.run_due(datetime(2026, 11, 2, 13, tzinfo=UTC)) == ()
    assert db.list_audit_events(entity_type="schedule")[0]["event_type"] == "scheduler.skipped"


def test_scheduler_rejects_malformed_factory_result():
    scheduler = ProposalScheduler()
    trigger = ScheduleTrigger("t", "UTC", timedelta(hours=1), datetime(2026, 11, 2, 10, tzinfo=UTC))
    scheduler.register(trigger, lambda **_: object())
    with pytest.raises(SchedulerError, match="StrategyProposal"):
        scheduler.run_due(datetime(2026, 11, 2, 10, tzinfo=UTC))


def test_dst_wall_clock_occurrence_is_timezone_aware():
    scheduler = ProposalScheduler()
    trigger = ScheduleTrigger("t", "America/New_York", timedelta(hours=1), datetime(2026, 3, 8, 0, tzinfo=ZoneInfo("America/New_York")), max_missed_runs=2)
    scheduler.register(trigger, factory)
    results = scheduler.run_due(datetime(2026, 3, 8, 7, 30, tzinfo=UTC))
    assert all(item.scheduled_at.tzinfo is not None for item in results)


def test_strategy_rejects_bad_ttl_and_future_indmoney_fact():
    now = datetime(2026, 11, 2, 10, tzinfo=UTC)
    with pytest.raises(StrategyInputError, match="TTL"):
        propose_kite_short_term(MarketFact("INFY", "INDIA", "INR", 1, now, "q", "buy"), PortfolioFact("p", "a", "kite", "INDIA", "INR"), now=now, ttl=timedelta(0))
