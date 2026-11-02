"""Timezone-aware, durable, fail-closed paper proposal scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .strategies import ProposalResult, StrategyProposal, NoTradeProposal
from .trading_models import ExecutionMode


class SchedulerError(ValueError):
    """Invalid schedule configuration, clock input, or factory result."""


@dataclass(frozen=True, slots=True)
class ScheduleTrigger:
    trigger_id: str
    timezone_name: str
    interval: timedelta
    start_at: datetime
    missed_run_policy: str = "coalesce"
    max_missed_runs: int = 1
    broker_id: str | None = None
    account_id: str | None = None
    portfolio_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_id, str) or not self.trigger_id:
            raise SchedulerError("trigger_id must be non-empty")
        if not isinstance(self.interval, timedelta) or self.interval <= timedelta(0):
            raise SchedulerError("interval must be positive")
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise SchedulerError("start_at must be timezone-aware")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise SchedulerError(f"unknown timezone: {self.timezone_name}") from exc
        if self.missed_run_policy not in {"coalesce", "skip"}:
            raise SchedulerError("missed_run_policy must be coalesce or skip")
        if not isinstance(self.max_missed_runs, int) or self.max_missed_runs < 0 or self.max_missed_runs > 10000:
            raise SchedulerError("max_missed_runs must be between 0 and 10000")


@dataclass(frozen=True, slots=True)
class ScheduledProposal:
    trigger_id: str
    scheduled_at: datetime
    result: ProposalResult


@dataclass(slots=True)
class _Entry:
    trigger: ScheduleTrigger
    factory: Callable[..., ProposalResult]
    last_index: int = -1


class ProposalScheduler:
    """Generate paper-only results; durable claims prevent replay across restarts."""

    def __init__(self, db=None) -> None:
        self.db = db
        if self.db is not None:
            self.db.init_db()
        self._entries: dict[str, _Entry] = {}
        self._completed: set[tuple[str, str]] = set()

    def register(self, trigger: ScheduleTrigger, factory: Callable[..., ProposalResult], **routing: str) -> None:
        if trigger.trigger_id in self._entries:
            raise SchedulerError(f"duplicate trigger_id: {trigger.trigger_id}")
        if not callable(factory):
            raise TypeError("factory must be callable")
        # Accept explicit routing at registration while retaining trigger compatibility.
        if routing:
            allowed = {"broker_id", "account_id", "portfolio_id"}
            if set(routing) - allowed:
                raise SchedulerError("unknown routing field")
            trigger = ScheduleTrigger(trigger.trigger_id, trigger.timezone_name, trigger.interval, trigger.start_at,
                trigger.missed_run_policy, trigger.max_missed_runs, routing.get("broker_id", trigger.broker_id),
                routing.get("account_id", trigger.account_id), routing.get("portfolio_id", trigger.portfolio_id))
        entry = _Entry(trigger, factory)
        if self.db is not None:
            with self.db.connect() as conn:
                row = conn.execute("SELECT last_occurrence_index FROM scheduler_state WHERE trigger_id=?", (trigger.trigger_id,)).fetchone()
                if row:
                    entry.last_index = int(row[0])
        self._entries[trigger.trigger_id] = entry

    @staticmethod
    def _local_occurrence(trigger: ScheduleTrigger, index: int) -> datetime:
        zone = ZoneInfo(trigger.timezone_name)
        start_local = trigger.start_at.astimezone(zone).replace(tzinfo=None)
        local = start_local + index * trigger.interval
        return local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)

    @staticmethod
    def _validate_result(result: Any, trigger: ScheduleTrigger) -> ProposalResult:
        if not isinstance(result, (StrategyProposal, NoTradeProposal)):
            raise SchedulerError("factory must return StrategyProposal or NoTradeProposal")
        metadata = result.metadata
        if not isinstance(metadata, dict) and not hasattr(metadata, "get"):
            raise SchedulerError("proposal metadata is required")
        if isinstance(result, StrategyProposal):
            intent = result.intent
            if intent.mode is not ExecutionMode.PAPER:
                raise SchedulerError("scheduler rejects non-paper proposals")
            if intent.broker_id is not None and intent.broker_id != metadata.get("broker_id"):
                raise SchedulerError("proposal broker binding is inconsistent")
        if metadata.get("mode") == "live":
            raise SchedulerError("scheduler rejects live proposals")
        for field in ("broker_id", "account_id", "portfolio_id"):
            expected = getattr(trigger, field)
            if expected is not None and metadata.get(field) != expected:
                raise SchedulerError(f"proposal {field} does not match trigger routing")
        if isinstance(result, StrategyProposal):
            for field in ("account_id", "portfolio_id"):
                expected = getattr(trigger, field)
                if expected is not None and getattr(result.intent, field) != expected:
                    raise SchedulerError(f"intent {field} does not match trigger routing")
        return result

    def run_due(self, now: datetime, **factory_kwargs: Any) -> tuple[ScheduledProposal, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise SchedulerError("now must be timezone-aware")
        generated: list[ScheduledProposal] = []
        for entry in self._entries.values():
            trigger = entry.trigger
            zone = ZoneInfo(trigger.timezone_name)
            now_local = now.astimezone(zone).replace(tzinfo=None)
            start_local = trigger.start_at.astimezone(zone).replace(tzinfo=None)
            if now_local < start_local:
                continue
            latest_index = int((now_local - start_local) // trigger.interval)
            if latest_index <= entry.last_index:
                continue
            if trigger.max_missed_runs == 0:
                indices: list[int] = []
            elif trigger.missed_run_policy == "skip":
                indices = [latest_index]
            else:
                first = max(entry.last_index + 1, latest_index - trigger.max_missed_runs + 1)
                indices = list(range(first, latest_index + 1))
            # Advance the cursor past all elapsed occurrences, even when no result is emitted.
            entry.last_index = latest_index
            claimed: list[tuple[int, str]] = []
            for index in indices:
                scheduled = self._local_occurrence(trigger, index)
                key = (trigger.trigger_id, scheduled.isoformat())
                if self.db is None:
                    if key in self._completed:
                        continue
                    self._completed.add(key)
                else:
                    with self.db.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        cur = conn.execute("INSERT OR IGNORE INTO scheduler_runs(trigger_id, scheduled_at, outcome, created_at) VALUES (?, ?, 'claimed', ?)", (trigger.trigger_id, scheduled.isoformat(), now.astimezone(timezone.utc).isoformat()))
                        conn.execute("INSERT INTO scheduler_state(trigger_id,last_scheduled_at,last_occurrence_index,updated_at) VALUES (?,?,?,?) ON CONFLICT(trigger_id) DO UPDATE SET last_scheduled_at=excluded.last_scheduled_at,last_occurrence_index=excluded.last_occurrence_index,updated_at=excluded.updated_at", (trigger.trigger_id, scheduled.isoformat(), latest_index, now.astimezone(timezone.utc).isoformat()))
                        if cur.rowcount == 0:
                            continue
                claimed.append((index, scheduled.isoformat()))
            if self.db is not None and not indices:
                with self.db.connect() as conn:
                    conn.execute("INSERT INTO scheduler_state(trigger_id,last_scheduled_at,last_occurrence_index,updated_at) VALUES (?,?,?,?) ON CONFLICT(trigger_id) DO UPDATE SET last_scheduled_at=excluded.last_scheduled_at,last_occurrence_index=excluded.last_occurrence_index,updated_at=excluded.updated_at", (trigger.trigger_id, self._local_occurrence(trigger, latest_index).isoformat(), latest_index, now.astimezone(timezone.utc).isoformat()))
                    self.db._append_audit_event_conn(conn, "scheduler.skipped", "schedule", trigger.trigger_id, {"scheduled_through": self._local_occurrence(trigger, latest_index).isoformat(), "reason": "max_missed_runs=0"}, trigger.broker_id, trigger.account_id, trigger.portfolio_id, "paper", now.isoformat())
            for index, scheduled_iso in claimed:
                scheduled = datetime.fromisoformat(scheduled_iso)
                try:
                    # Freshness and TTL use actual current time, never scheduled_at.
                    result = self._validate_result(entry.factory(now=now.astimezone(zone), **factory_kwargs), trigger)
                    if self.db is not None:
                        with self.db.connect() as conn:
                            conn.execute("UPDATE scheduler_runs SET outcome='generated' WHERE trigger_id=? AND scheduled_at=?", (trigger.trigger_id, scheduled_iso))
                            self.db._append_audit_event_conn(conn, "scheduler.generated", "schedule", trigger.trigger_id, {"scheduled_at": scheduled_iso}, trigger.broker_id, trigger.account_id, trigger.portfolio_id, "paper", now.isoformat())
                    generated.append(ScheduledProposal(trigger.trigger_id, scheduled, result))
                except Exception as exc:
                    if self.db is not None:
                        with self.db.connect() as conn:
                            conn.execute("UPDATE scheduler_runs SET outcome='failed' WHERE trigger_id=? AND scheduled_at=?", (trigger.trigger_id, scheduled_iso))
                            self.db._append_audit_event_conn(conn, "scheduler.failed", "schedule", trigger.trigger_id, {"scheduled_at": scheduled_iso, "error": str(exc)}, trigger.broker_id, trigger.account_id, trigger.portfolio_id, "paper", now.isoformat())
                    if isinstance(exc, SchedulerError):
                        raise
                    raise SchedulerError(f"factory failed for {trigger.trigger_id}") from exc
        return tuple(generated)

    def clear_completed(self) -> None:
        """True reset: clear dedupe and rewind durable cursors."""
        self._completed.clear()
        for entry in self._entries.values():
            entry.last_index = -1
        if self.db is not None:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM scheduler_runs")
                conn.execute("DELETE FROM scheduler_state")

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)
