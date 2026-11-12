# Bloomberg-Inspired Functional Console Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make every operator page a dense, functional terminal workspace while preserving the existing validated APIs and paper-first safety boundary.

**Architecture:** Keep Flask routes and backend models. Add a terminal shell to `web.py` with real server-rendered portfolio data, JSON-backed panel actions, keyboard command palette, auto-refresh, and links to existing operator pages. Add focused tests for rendered controls, commands, and safe actions.

**Tech Stack:** Flask/Jinja, vanilla HTML/CSS/JS, existing SQLite APIs, pytest.

---

### Task 1: Build terminal shell around existing portfolio data

**Files:** `portfolio_tracker/web.py`, `tests/test_operator_console.py`

Implement a Bloomberg-inspired dashboard with left navigation, top command bar, market/watchlist panels from available local data, portfolio blotter, risk summary, agent status, integration status, and event footer. Do not fake broker connectivity; label unavailable data honestly. Preserve all existing forms and routes.

Acceptance: authenticated `/` renders terminal sections and real holdings/transactions; no raw JSON-only panels; responsive layout.

### Task 2: Add functional terminal actions and command palette

**Files:** `portfolio_tracker/web.py`, `tests/test_operator_console.py`

Add command palette UI with safe commands that navigate or call existing endpoints: portfolio, agents, trades, risk, integrations, scheduler, audit, settings, run risk review, enable kill switch. Add keyboard shortcut Ctrl/Cmd+K, visible result/toast area, and CSRF headers for mutations.

Acceptance: commands perform real validated actions or navigation; arbitrary commands are rejected; mutation errors are shown.

### Task 3: Add dashboard polling and panel refresh

**Files:** `portfolio_tracker/web.py`, `tests/test_operator_console.py`

Add periodic polling of authenticated JSON endpoints for portfolio, agents, integrations, audit/risk state. Update dashboard panels without full-page navigation; show last refresh and stale/error state. Keep polling bounded and stop when tab is hidden.

Acceptance: dashboard remains usable with API failure; no credentials or secrets appear in DOM; tests cover rendered refresh endpoints.

### Task 4: Verify safety and deploy

Run focused tests, full pytest suite, compileall, git diff check, build/deploy Unraid container, and verify health plus authenticated dashboard controls through HTTP.
