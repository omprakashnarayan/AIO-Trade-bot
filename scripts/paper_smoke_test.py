#!/usr/bin/env python3
"""Run a deterministic, local-only paper-trading execution scenario."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

# Allow ``python scripts/paper_smoke_test.py`` from a repository checkout.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_tracker.db import Database
from portfolio_tracker.execution import ExecutionGateway
from portfolio_tracker.paper_broker import PaperBroker
from portfolio_tracker.risk import RiskEngine, RiskLimits
from portfolio_tracker.trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce


ACCOUNT = "smoke-account"
PORTFOLIO = "smoke-portfolio"
SYMBOL = "INFY"
CURRENCY = "INR"


def intent(key: str, side: str, quantity: str) -> OrderIntent:
    return OrderIntent(
        idempotency_key=key,
        portfolio_id=PORTFOLIO,
        symbol=SYMBOL,
        side=OrderSide(side),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=quantity,
        currency=CURRENCY,
        mode=ExecutionMode.PAPER,
        account_id=ACCOUNT,
        broker_id="paper",
    )


def snapshot(db: Database) -> tuple[object, ...]:
    cash = db.get_cash_balance(
        currency=CURRENCY,
        account_id=ACCOUNT,
        portfolio_id=PORTFOLIO,
        broker_id="paper",
    )
    holding = db.get_paper_holding(
        broker_id="paper", account_id=ACCOUNT, portfolio=PORTFOLIO, symbol=SYMBOL
    )
    return (
        tuple(tuple(row) for row in db.list_order_intents()),
        tuple(tuple(row) for row in db.list_fills()),
        tuple(tuple(row) for row in db.list_cash_ledger(broker_id="paper")),
        tuple(tuple(row) for row in db.list_paper_transactions(broker_id="paper")),
        tuple(cash) if cash else None,
        tuple(holding) if holding else None,
    )


def run() -> None:
    # A temporary SQLite database makes every run isolated and prevents any
    # accidental interaction with configured databases or broker credentials.
    with TemporaryDirectory(
        prefix="portfolio-paper-smoke-", ignore_cleanup_errors=True
    ) as directory:
        db = Database(Path(directory) / "smoke.sqlite")
        broker = PaperBroker(db)
        broker.seed_cash(
            "10000", currency=CURRENCY, account_id=ACCOUNT, portfolio_id=PORTFOLIO
        )
        gateway = ExecutionGateway(
            db,
            risk=RiskEngine(db=db, limits=RiskLimits()),
            paper_broker=broker,
        )

        buy = gateway.submit(intent("smoke-buy-1", "buy", "2"), quote_price="100")
        assert buy.status == "submitted" and buy.broker_result is not None
        assert buy.broker_result.filled and not buy.broker_result.reused

        retry = gateway.submit(
            intent("smoke-buy-1", "buy", "2"), quote_price="999"
        )
        assert retry.status == "submitted" and retry.broker_result is not None
        assert retry.broker_result.reused
        assert len(db.list_fills()) == 1
        assert len(db.list_cash_ledger(broker_id="paper")) == 1

        before_conflict = snapshot(db)
        conflict = gateway.submit(
            intent("smoke-buy-1", "buy", "1"), quote_price="100"
        )
        assert conflict.status == "rejected"
        assert conflict.risk.code == "conflicting_idempotency"
        assert snapshot(db) == before_conflict

        sell = gateway.submit(intent("smoke-sell-1", "sell", "1"), quote_price="110")
        assert sell.status == "submitted" and sell.broker_result is not None
        assert sell.broker_result.filled and not sell.broker_result.reused

        cash = db.get_cash_balance(
            currency=CURRENCY,
            account_id=ACCOUNT,
            portfolio_id=PORTFOLIO,
            broker_id="paper",
        )
        holding = db.get_paper_holding(
            broker_id="paper", account_id=ACCOUNT, portfolio=PORTFOLIO, symbol=SYMBOL
        )
        assert cash is not None and Decimal(cash["balance"]) == Decimal("9910.000000000000000000")
        assert holding is not None and Decimal(holding["shares"]) == Decimal("1")
        assert len(db.list_order_intents()) == 2
        assert len(db.list_fills()) == 2

        events = db.list_audit_events()
        event_names = [row["event_type"] for row in events]
        print("paper smoke test: PASS")
        print("orders=2 fills=2 cash=9910.000000000000000000 INR position=INFY:1")
        print(f"audit_events={len(events)} events={','.join(event_names)}")


def main() -> int:
    try:
        run()
    except AssertionError as exc:
        print(f"paper smoke test: FAIL ({exc})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"paper smoke test: ERROR ({exc})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
