"""Paper-only, broker-neutral execution gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .db import Database
from .paper_broker import PaperBroker, PaperExecutionResult
from .risk import RiskDecision, RiskEngine
from .trading_models import ExecutionMode, OrderIntent


class ExecutionError(Exception):
    """Base execution gateway error."""


class LiveModeRejected(ExecutionError):
    """Live execution is deliberately fail-closed until a live adapter exists."""


LiveModeError = LiveModeRejected


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    status: str
    risk: RiskDecision
    broker_result: PaperExecutionResult | None = None
    error: str | None = None

    @property
    def submitted(self) -> bool:
        return self.broker_result is not None


ExecutionResult = ExecutionDecision


class ExecutionGateway:
    """The only path from a validated intent to the current paper broker."""

    def __init__(self, db: Database, *, risk: RiskEngine | None = None, risk_engine: RiskEngine | None = None, paper_broker: PaperBroker | None = None) -> None:
        self.db = db
        self.db.init_db()
        if risk is not None and risk_engine is not None:
            raise ValueError("provide only one of risk or risk_engine")
        self.risk = risk or risk_engine or RiskEngine(db=db)
        self.paper_broker = paper_broker or PaperBroker(db)

    def submit(
        self,
        intent: OrderIntent,
        *,
        quote_price: Decimal | int | str,
        quote_timestamp: datetime | None = None,
        portfolio_value: Decimal | int | str | None = None,
        current_position_notional: Decimal | int | str = Decimal("0"),
        market_open: bool | None = None,
        session_allowed: bool | None = None,
    ) -> ExecutionDecision:
        if not isinstance(intent, OrderIntent):
            raise TypeError("ExecutionGateway accepts only a validated OrderIntent")
        if intent.mode is ExecutionMode.LIVE:
            risk = RiskDecision(False, "live_mode", "LIVE mode is disabled; no live broker is configured", {})
            self._report(intent, "execution.live_rejected", risk)
            raise LiveModeRejected(risk.message)
        if intent.mode is not ExecutionMode.PAPER:
            raise ExecutionError("unsupported execution mode")
        risk = self.risk.check(
            intent,
            quote_price=quote_price,
            quote_timestamp=quote_timestamp,
            portfolio_value=portfolio_value,
            current_position_notional=current_position_notional,
            market_open=market_open,
            session_allowed=session_allowed,
            fee_rate=self.paper_broker.fee_rate,
            slippage_bps=self.paper_broker.slippage_bps,
            broker_id=self.paper_broker.broker_id,
        )
        if not risk.allowed:
            self._report(intent, "execution.risk_rejected", risk)
            return ExecutionDecision("rejected", risk, error=risk.message)
        self._report(intent, "execution.risk_approved", risk)
        try:
            result = self.paper_broker.submit(intent, quote_price, max_position_weight=self.risk.limits.max_position_weight, portfolio_value=portfolio_value)
        except Exception as exc:
            try:
                self._report(intent, "execution.failed", risk, {"error": str(exc), "reconciliation_required": True})
            except Exception:
                pass
            raise
        status = "submitted" if result.status == "filled" else result.status
        try:
            self._report(intent, "execution.paper_result", risk, {"status": result.status, "reused": result.reused, "order_id": result.order_id})
        except Exception as exc:
            return ExecutionDecision("reconciliation_required", risk, result, f"execution completed but audit write failed: {exc}")
        return ExecutionDecision(status, risk, result, result.error)

    execute = submit
    submit_order = submit

    def _report(self, intent: OrderIntent, event_type: str, risk: RiskDecision, details: dict[str, Any] | None = None) -> None:
        payload = {"decision": {"allowed": risk.allowed, "code": risk.code, "message": risk.message, "details": risk.details}}
        if details:
            payload["execution"] = details
        self.db.append_audit_event(
            event_type=event_type,
            entity_type="order_intent",
            entity_id=intent.idempotency_key,
            payload=payload,
            broker_id="paper",
            account_id=intent.account_id,
            portfolio_id=intent.portfolio_id,
            mode=intent.mode.value,
        )
