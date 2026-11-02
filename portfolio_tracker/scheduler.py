"""Timezone-aware, deduplicating paper proposal scheduler.

The scheduler invokes proposal factories only.  It has no broker, credential,
or order-submission dependency by design.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .strategies import ProposalResult


class SchedulerError(ValueError):
    """Invalid schedule configuration or clock input."""


@dataclass(frozen=True, slots=True)
class ScheduleTrigger:
    trigger_id: str
    timezone_name: str
    interval: timedelta
    start_at: datetime
    missed_run_policy: str = "coalesce"
    max_missed_runs: int = 1

    def __post_init__(self) -> None:
        if not self.trigger_id or not isinstance(self.trigger_id, str):
            raise SchedulerError("trigger_id must be non-empty")
        if self.interval <= timedelta(0):
            raise SchedulerError("interval must be positive")
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise SchedulerError("start_at must be timezone-aware")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise SchedulerError(f"unknown timezone: {self.timezone_name}") from exc
        if self.missed_run_policy not in {"coalesce", "skip"}:
            raise SchedulerError("missed_run_policy must be coalesce or skip")
        if self.max_missed_runs < 0:
            raise SchedulerError("max_missed_runs must be nonnegative")


@dataclass(frozen=True, slots=True)
class ScheduledProposal:
    trigger_id: str
    scheduled_at: datetime
    result: ProposalResult


@dataclass(slots=True)
class _Entry:
    trigger: ScheduleTrigger
    factory: Callable[..., ProposalResult]
    last_scheduled_utc: datetime | None = None


class ProposalScheduler:
    """Generate at most one result per trigger occurrence, in paper mode only."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._completed: set[tuple[str, datetime]] = set()

    def register(self, trigger: ScheduleTrigger, factory: Callable[..., ProposalResult]) -> None:
        if trigger.trigger_id in self._entries:
            raise SchedulerError(f"duplicate trigger_id: {trigger.trigger_id}")
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._entries[trigger.trigger_id] = _Entry(trigger, factory)

    def run_due(self, now: datetime, **factory_kwargs: Any) -> tuple[ScheduledProposal, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise SchedulerError("now must be timezone-aware")
        now_utc = now.astimezone(timezone.utc)
        generated: list[ScheduledProposal] = []
        for entry in self._entries.values():
            trigger = entry.trigger
            start_utc = trigger.start_at.astimezone(timezone.utc)
            if now_utc < start_utc:
                continue
            last = entry.last_scheduled_utc
            if last is None:
                due = [start_utc]
            else:
                elapsed = now_utc - last
                count = int(elapsed // trigger.interval)
                if count <= 0:
                    continue
                if trigger.missed_run_policy == "skip":
                    due = [last + count * trigger.interval]
                else:
                    count = min(count, max(1, trigger.max_missed_runs))
                    first = last + trigger.interval
                    due = [first + index * trigger.interval for index in range(count)]
            for scheduled_utc in due:
                key = (trigger.trigger_id, scheduled_utc)
                if key in self._completed:
                    continue
                # The factory is deliberately passed no scheduler or broker object.
                result = entry.factory(now=scheduled_utc.astimezone(ZoneInfo(trigger.timezone_name)), **factory_kwargs)
                self._completed.add(key)
                generated.append(ScheduledProposal(trigger.trigger_id, scheduled_utc, result))
            if due:
                entry.last_scheduled_utc = due[-1]
        return tuple(generated)

    def clear_completed(self) -> None:
        """Forget deduplication history; useful only for a deliberate schedule reset."""
        self._completed.clear()

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)
