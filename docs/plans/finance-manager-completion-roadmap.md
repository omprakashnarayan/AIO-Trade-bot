# Finance Manager Completion Roadmap

> **For Hermes:** Execute sequentially with paper-first gates. Every external read must be verified before claiming populated data.

**Goal:** Turn the current portfolio operator console into a unified, AI-assisted personal finance manager for Kite, Upstox, INDmoney, manual holdings, and paper trading.

**Safety:** Real broker reads are allowed only through authenticated provider adapters/MCP. Live writes stay disabled. Manual, broker, imported, and paper data remain separate namespaces.

## Current status

- UI shell, authentication, terminal pages, agent controls, paper broker, risk engine, scheduler, OAuth callback handling, encrypted token storage, manual holdings, and INDmoney CSV import exist.
- Provider authorization exists, but actual provider read/sync and reconciliation are not wired into portfolio storage.
- Market data, tax lots, performance analytics, goals, alerts, and evidence-backed AI research are incomplete.

## Implementation order

1. **Provider data plane:** typed read adapters, broker-scoped snapshots, sync jobs, reconciliation, source labels, last-sync/error state.
2. **Ledger and cost basis:** normalized transactions, lots, dividends, fees, corporate actions, realized/unrealized P/L, FX conversion, tax exports.
3. **Market data:** verified quote/history provider, benchmarks, charts, freshness, market calendar, news/fundamental boundaries.
4. **Portfolio analytics:** XIRR/TWR, drawdown, exposure, sector/country/currency concentration, benchmark comparison.
5. **AI research:** evidence-linked findings, portfolio health, risk, rebalance, earnings/news, daily briefing, feedback history.
6. **Manual finance workflows:** imports, correction/reversal, attachments/provenance, cash flows, liabilities, goals, contributions.
7. **Alerts and operations:** configurable alerts, sync failures, OAuth expiry, risk breaches, agent failures, backups/restores.
8. **Paper operations:** paper order lifecycle, fill/reconciliation, strategy replay/backtesting, approval records, daily reports.
9. **Promotion gates:** broker sandbox/replay/fault tests, separate credentials, human approval, kill switch, recovery procedure. Live writes remain off until explicitly reviewed.

## Acceptance gates

- Each broker sync has one real authorized read verified in a controlled test.
- Every provider record has broker/account/source/timestamp scope.
- Sync is idempotent and does not silently overwrite manual or paper state.
- Provider errors are redacted, visible, retryable, and audited.
- AI outputs include evidence, timestamps, risks, invalidation conditions, and strategy version.
- Manual workflows remain available when providers are unavailable.
- Full tests, migration checks, paper smoke tests, and deployment verification pass after every phase.
