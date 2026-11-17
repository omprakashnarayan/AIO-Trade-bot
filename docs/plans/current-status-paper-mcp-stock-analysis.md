# Dashboard Status, Paper MCP Actions, and Stock Analysis Plan

> **For Hermes:** Execute sequentially with TDD and the paper-first promotion gates below.

**Goal:** Show current source/workspace coverage and saved goals on the dashboard, add safely audited paper-trading/risk controls to the local application MCP, and provide a stock-analysis workspace for existing or new tickers using the existing evidence-gated research agent.

**Architecture:** Keep broker provider MCP integrations read-only and namespace-separated. The local FinDash application MCP may mutate only validated paper orders, kill-switch state, scheduler state, and allowlisted agent state/run controls behind its bearer-authenticated tool boundary. The Stock Analysis page uses existing stored holdings, quote/evidence snapshots and the existing `market_research` agent; it must state unavailable when current verified evidence is absent and never invent market facts.

**Safety:** No provider order/cancel/write tools, no live-order tool, no copying broker snapshots into manual/paper trades, and no enabling LIVE execution. Preserve the execution gateway's independent LIVE rejection, CSRF for browser-session mutations, MCP bearer auth for app-MCP mutations, audit events, and broker/account/currency provenance.

**Tech Stack:** Flask/Jinja, vanilla JavaScript, SQLite, existing MCPServer/ExecutionGateway/agent registry, pytest.

---

### Task 1: Show dashboard source coverage and goals

**Files:** `portfolio_tracker/web.py`, `tests/test_provider_display.py`, optionally `tests/test_operator_console.py`.

- Add a read-only coverage payload to `_console_snapshot()` with per-provider counts for holdings/positions/cash/orders/fills and latest sync/reconciliation status.
- Add workspace counts/statuses for manual holdings, paper orders, ledger events, research runs/evidence/findings, finance goals/cash flow/liabilities/alerts, scheduler runs, and audit records. Label an empty source distinctly from an unavailable source, and explain that a broker holdings snapshot does not create a trade or ledger event.
- Add a dashboard panel with links to the corresponding pages, native broker/account scope, and a goals/progress summary or an explicit “no goals saved” state with a Finance link.
- Include this status in `/api/console/snapshot`; dashboard polling must refresh/reload visible coverage when the status payload changes.

**TDD:** Extend `test_provider_display.py` with seeded broker snapshots plus isolated workspace rows. Assert exact counts, empty reasons, goals, links, separation, and poll wiring; run it red before implementation, then green.

### Task 2: Add safe local app-MCP paper/risk actions

**Files:** `portfolio_tracker/mcp_server.py`, new `tests/test_mcp_actions.py`, only adjust auth/deployment files if required.

- Keep existing `paper_order_submit` as the only trade submission path; expose typed buy/sell paper orders with explicit account/portfolio/currency/idempotency and deterministic risk checks.
- Add narrowly typed tools for global paper kill-switch on/off, scheduler enable/disable, and allowlisted local agent enable/disable/run. Every mutation must be bearer-authenticated through `/mcp`, validate enums/booleans/names, persist and audit its result, and return a bounded status payload.
- Do not add hosted broker write tools or any LIVE capability. Keep user confirmation on browser UI actions where already present; MCP mutations are explicit authenticated calls and must remain paper-only.

**TDD/security tests:** Verify tools/list schemas and descriptions; successful buy/sell through ExecutionGateway; idempotency; insufficient cash/risk and kill-switch rejection; live mode rejection; agent allowlist/disabled behavior; scheduler and kill-switch audit; unauthenticated HTTP 401; no session CSRF dependency for bearer MCP; no provider-write methods exposed.

### Task 3: Add Stock Analysis workspace backed by the existing agent

**Files:** `portfolio_tracker/web.py`, possibly `portfolio_tracker/agents.py` only if a narrowly scoped adapter is needed, and tests in `tests/test_provider_display.py` or a new `tests/test_stock_analysis_ui.py`.

- Add a navigation item and `/page/stock-analysis` UI for an existing holding or a new ticker. Show manual holdings and broker snapshot matches separately, plus currency/source/time for stored quotes and verified research evidence.
- Add a CSRF-protected “Analyze” action that invokes the existing `market_research` agent with the selected symbol and only current verified evidence IDs. Persist and display run/finding/status, confidence, risks, and invalidation conditions.
- With no current verified evidence, show “UNAVAILABLE — no current verified evidence” and no recommendation; do not fetch guessed prices, scrape accounts, claim fundamentals/news, or create synthetic findings. Existing/new ticker choice changes only scope, not evidence standards.

**TDD:** Test existing and new symbols, evidence scoping and freshness, unavailable behavior, output provenance, CSRF, and that analysis does not modify broker/manual/paper holdings.

### Task 4: Review and promotion gates

- Run focused TDD suites, full repository suite, compile/diff checks, rendered JavaScript syntax checks, and authenticated desktop/narrow-view smoke checks.
- Dispatch spec and security reviews sequentially for each implementation slice; fix and re-review blockers.
- Back up the persistent database, verify protected deployment secrets without printing them, build/deploy the Unraid container, and verify public authenticated dashboard, app-MCP 401/bearer flows, paper-only order and risk controls, and stock-analysis unavailable/evidence-backed behavior.
- Commit and push only the reviewed changes. Live trading must remain disabled and rejected by the execution gateway.
