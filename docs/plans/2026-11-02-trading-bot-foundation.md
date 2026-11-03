# US and India Trading Bot Foundation Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Extend the existing portfolio tracker into a paper-first, broker-neutral trading system for Indian short-term trading through Zerodha Kite Connect, Indian long-term investing through Upstox, and read-only/manual tracking of US investments held through the INDmoney consumer app.

**Architecture:** Keep research and AI agents strictly separate from execution. Agents create typed, expiring trade proposals; deterministic risk code validates them; a broker-neutral execution gateway handles paper orders and later approved live adapters. INDmoney consumer-app holdings are imported or entered manually because consumer-app automation is not an approved execution API.

**Tech Stack:** Python 3.13, SQLite, Decimal, pytest, broker SDK/REST adapters, scheduled CLI jobs, optional Flask UI.

---

### Task 1: Add typed trading domain models and explicit modes

**Objective:** Define immutable order intents, proposals, fills, and paper/live mode values without touching broker APIs.

**Files:**
- Create: `portfolio_tracker/trading_models.py`
- Test: `tests/test_trading_models.py`

**Acceptance:** Invalid side, quantity, currency, mode, or missing idempotency key is rejected; valid long-only equity intents serialize deterministically.

### Task 2: Add SQLite order, fill, cash, and audit tables

**Objective:** Persist paper-trading state and audit events independently of manual holdings.

**Files:**
- Modify: `portfolio_tracker/db.py`
- Create: `tests/test_trading_db.py`

**Acceptance:** Schema initializes idempotently; order intents and audit events can be written/read; duplicate idempotency keys are rejected.

### Task 3: Implement a deterministic PaperBroker

**Objective:** Provide working paper execution with cash checks, long-only position checks, configurable fees/slippage, and deterministic fills.

**Files:**
- Create: `portfolio_tracker/paper_broker.py`
- Create: `tests/test_paper_broker.py`

**Acceptance:** Buy/sell orders fill deterministically, insufficient cash/position is rejected, repeat submission with the same idempotency key does not duplicate a fill, and every result is audited.

### Task 4: Add broker-neutral execution gateway and risk limits

**Objective:** Ensure only validated typed intents can reach a broker implementation.

**Files:**
- Create: `portfolio_tracker/execution.py`
- Create: `portfolio_tracker/risk.py`
- Create: `tests/test_execution.py`
- Create: `tests/test_risk.py`

**Acceptance:** Paper mode is default; live mode is fail-closed; max order notional, max position weight, stale price, duplicate intent, and kill switch are enforced.

### Task 5: Add India broker adapter interfaces

**Objective:** Define adapter contracts and implement API clients behind explicit configuration, without enabling live calls by default.

**Files:**
- Create: `portfolio_tracker/brokers/base.py`
- Create: `portfolio_tracker/brokers/kite.py`
- Create: `portfolio_tracker/brokers/upstox.py`
- Create: `tests/test_broker_adapters.py`

**Acceptance:** Adapters validate configuration and map provider errors/statuses into internal types; tests use mocked HTTP only; no secrets are committed.

### Task 6: Add INDmoney holdings import path

**Objective:** Track US assets held in the INDmoney consumer app without pretending it supports direct automated order execution.

**Files:**
- Create: `portfolio_tracker/imports/indmoney.py`
- Create: `tests/test_indmoney_import.py`
- Modify: `portfolio_tracker/cli.py`

**Acceptance:** CSV/manual import updates a dedicated US portfolio with USD currency and source metadata; no broker order method exists for this integration.

### Task 7: Add strategy proposal generation and scheduled triggers

**Objective:** Allow short-term Kite proposals and long-term Upstox proposals to be generated on schedules while preserving approval and risk gates.

**Files:**
- Create: `portfolio_tracker/strategies.py`
- Create: `portfolio_tracker/scheduler.py`
- Create: `tests/test_strategies.py`

**Acceptance:** Strategies emit proposals only, with explicit strategy/version/expiry and no broker access; scheduler deduplicates runs.

### Task 8: Verify paper operation end to end

**Objective:** Run unit tests and a deterministic CLI paper-trading scenario before any live adapter is activated.

**Files:**
- Create: `scripts/paper_smoke_test.py`
- Modify: `README.md`

**Acceptance:** Local paper smoke test creates a buy, rejects a duplicate, sells within position limits, and prints the audit trail; all tests pass.

### Live-trading promotion gate

Live execution remains disabled until the user supplies the specific US execution broker/API, confirms account ownership and permitted instruments, configures provider credentials through a secret store, verifies static-IP/OAuth/TOTP requirements, completes paper soak testing, and explicitly enables live mode. The system must start with human approval on every order and tiny notional limits.
