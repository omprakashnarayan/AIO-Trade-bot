# Portfolio Tracker

A local portfolio tracker with a paper-first trading foundation. The project keeps research and portfolio imports separate from deterministic order execution, and does not enable live order writes.

## Architecture

- **Domain models** (`portfolio_tracker/trading_models.py`) validate immutable, typed order intents and proposals.
- **Risk engine** (`portfolio_tracker/risk.py`) checks kill switches, quote/session rules, order limits, position limits, and scoped idempotency before execution.
- **Execution gateway** (`portfolio_tracker/execution.py`) is the only route from a validated intent to execution. It is paper-only today and fails closed for LIVE intents.
- **Paper broker** (`portfolio_tracker/paper_broker.py`) executes deterministic long-only equity market orders against a local SQLite database using `Decimal` values. Cash, holdings, fills, transactions, and append-only audit events are broker/account/portfolio scoped.
- **Broker adapters** (`portfolio_tracker/brokers/`) define provider boundaries for Kite and Upstox. They support read/status/reconciliation surfaces through an injectable transport, but live writes remain disabled.
- **INDmoney importer** (`portfolio_tracker/imports/indmoney.py`) imports manually supplied CSV or JSON records into the dedicated `INDmoney-US` USD portfolio. It is read-only; it is not an order adapter.
- **Strategies and scheduler** produce expiring proposals only. They do not hold credentials or submit orders.

No smoke test or paper execution path uses a network, live adapter, broker credential, or configured production database.

No US execution broker/API has been selected or configured; INDmoney is read-only for this project.

## Docker deployment

This host does not provide the Docker Compose plugin, so deploy with the included Docker CLI script. It builds the image, creates the persistent data volume, replaces the container, enables restart-on-boot, and waits for the health check:

```bash
./scripts/deploy_docker.sh
```

Defaults:

- Image: `portfolio-tracker:local`
- Container: `portfolio-tracker`
- Persistent volume: `portfolio_tracker_data`
- Web port: `5055`

Override values when needed:

```bash
IMAGE=registry.example/portfolio-tracker:latest PORT=5055 ./scripts/deploy_docker.sh
```

The container stores its SQLite database and reports in `/data` and runs as a non-root user with a read-only root filesystem. On Unraid, use the host-path deployment script so state is stored on the array/cache under `/mnt/user/appdata/portfolio-tracker`:

```bash
./scripts/deploy_unraid.sh
```

Override the Unraid appdata location if needed:

```bash
APPDATA_DIR=/mnt/user/appdata/aiotradebot ./scripts/deploy_unraid.sh
```

Verify deployment with:

```bash
docker ps --filter name=portfolio-tracker
docker inspect --format '{{.State.Health.Status}}' portfolio-tracker
curl -fsS http://127.0.0.1:5055/api/report
```

## Setup and tests

From the repository root:

```bash
./.venv/bin/pytest -q
./.venv/bin/python -m compileall -q portfolio_tracker scripts
./.venv/bin/python scripts/paper_smoke_test.py
```

The smoke test creates an isolated temporary SQLite database, seeds INR paper cash, submits a buy, retries the identical intent with a different quote to verify idempotency, rejects a conflicting same-key intent without changing trade state, submits a sell, and prints the resulting audit/state summary. Assertions make failures exit with status 1.

## CLI

Initialize the local application database/settings:

```bash
./.venv/bin/portfolio-tracker init
```

Import manually exported INDmoney US holdings. The importer accepts a CSV path/text or a JSON array/path and always writes to the dedicated USD `INDmoney-US` portfolio:

```bash
./.venv/bin/portfolio-tracker import-indmoney \
  --csv ./indmoney_holdings.csv \
  --source indmoney-export

./.venv/bin/portfolio-tracker import-indmoney \
  --records '[{"symbol":"AAPL","shares":"2","average_cost":"180"}]'
```

The importer is deliberately read-only and records source/import timestamps and provenance. It does not place orders through INDmoney.

## Current broker limitations

- **Paper execution is the only enabled order path.** `ExecutionGateway` rejects LIVE intents, and `PaperBroker` rejects non-paper mode.
- **Kite live writes are disabled.** The Kite adapter's order and cancel write boundary remains fail-closed; route paper intents through the gateway.
- **Upstox live writes are disabled.** The Upstox adapter inherits the same fail-closed write boundary.
- Kite and Upstox have no production transport configured by default. Any adapter transport used in tests must be injected explicitly and must not contain real credentials.
- The paper broker currently supports long-only equity market BUY and SELL orders. It is not a market simulator: it does not model exchange matching, partial fills, market hours, corporate actions, or provider reconciliation.
- **INDmoney is read-only.** Consumer-app holdings are imported or entered manually; there is no supported INDmoney trading API integration here.
- Paper state is separate from manual holdings and imported INDmoney state. It must not be interpreted as a live account statement.

## Explicit live-trading promotion gates

Live trading must remain disabled until every gate below is completed and documented:

1. Select and approve a specific supported execution API and confirm account ownership, permitted instruments, exchanges, currency, and legal/tax/compliance obligations.
2. Implement and independently test the live adapter's order, cancel/replace, status, fill, and reconciliation behavior against an official sandbox or broker test environment.
3. Store separate paper and live credentials in a secret store. Never place credentials in source, config files, tests, smoke scripts, or agent prompts.
4. Verify OAuth/TOTP/token-expiry behavior immediately before every live write, plus provider rate limits, static-IP/allow-list requirements, market calendars, and failure semantics.
5. Complete a defined paper soak with a minimum order sample, deterministic replay tests, network/provider fault injection, reconciliation checks, and zero unknown orders or fills.
6. Set and test hard limits for order notional, position and portfolio weight, turnover, slippage, daily loss, drawdown, concentration, and currency/market exposure.
7. Keep live storage and audit namespaces separate from paper storage; prove idempotent retries, conflict rejection, rollback behavior, and restart/concurrency safety.
8. Test a gateway kill switch, manual flatten procedure, alerting, operator runbook, and recovery/reconciliation workflow.
9. Require explicit human approval for each initial live order and begin with tiny notional limits. If any gate is unknown or fails, fail closed and continue in paper or read-only mode.

Until these gates are satisfied, Kite and Upstox live writes remain disabled and INDmoney remains read-only.
