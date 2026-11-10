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

Override the Unraid appdata location or host IP if needed:

```bash
APPDATA_DIR=/mnt/user/appdata/aiotradebot HOST_IP=192.168.200.196 ./scripts/deploy_unraid.sh
```

Verify deployment with:

```bash
docker ps --filter name=portfolio-tracker
docker inspect --format '{{.State.Health.Status}}' portfolio-tracker
curl -fsS http://127.0.0.1:5055/api/report
```

## MCP integration boundary

The dashboard exposes local CRUD for manual holdings and settings. Remote app data should be accessed through native MCP connections, but MCP is treated as a transport—not as a risk engine.

A reviewed example configuration is provided at `config/mcp_servers.example.yaml` for Kite, Upstox, and INDmoney. Copy only its `mcp_servers` block into the Hermes Agent config after reviewing the provider authorization flow and discovered tools. Do not commit tokens or credentials.

Current safety policy:

- Kite and Upstox data can be read through approved MCP tools; any order proposal must be converted into a typed paper intent and pass the execution gateway.
- INDmoney remains read-only holdings visibility/import; no buy, sell, transfer, or account mutation is exposed.
- The dashboard's local CRUD endpoints mutate only the local SQLite-backed manual portfolio, not remote broker accounts.
- Provider connectivity is not claimed until the MCP server is authorized and a real read call is verified.

The dashboard shows this distinction explicitly in its Integrations panel.


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

## Operator console security and usage

The Flask console is locked until all required admin variables are present. It never accepts or stores a plaintext password. Generate a hash interactively on the Unraid host (the plaintext is not written to shell history):

```bash
python3 -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))'
```

Set these exact Unraid container environment variables and restart the container:

```text
PORTFOLIO_ADMIN_USERNAME=admin
PORTFOLIO_ADMIN_PASSWORD_HASH=<output from the command above>
PORTFOLIO_SESSION_SECRET=<random 32+ byte secret>
PORTFOLIO_ALLOWED_CIDRS=192.168.200.0/24,127.0.0.1/32
# Optional connector declarations only; this console makes no provider calls:
KITE_MCP_ENDPOINT=
UPSTOX_MCP_ENDPOINT=
INDMONEY_MCP_ENDPOINT=
```

All trading configuration—trading mode, risk limits, scheduler enabled state, default account/portfolio/currency, and strategy preferences—is changed from the authenticated Settings UI and persisted under `/data/settings.json`. It is not read from Docker environment variables. Docker environment variables are limited to deployment, authentication, network restriction, and connector declarations.

`PORTFOLIO_ALLOWED_CIDRS` is mandatory for LAN access. If omitted, only loopback (`127.0.0.1` and `::1`) is allowed; the application does not guess the Unraid subnet. Replace `192.168.200.0/24` with the actual LAN CIDR, and do not include a public or Docker-wide range.

Open `http://<unraid-host>:5055/`, sign in, and use the navigation for Portfolio, **Agents**, Trades, Integrations, Strategies, Scheduler, Audit/Risk, and Settings. The Agents page is the user-facing control plane: enable/disable each built-in agent, run now, run a test/dry-run, retry its last durable run, and view stored logs/results. No agent behavior requires editing Docker environment variables or config files. Paper orders are submitted only through `ExecutionGateway` and are subject to risk checks, cash/position checks, idempotency, and the persistent global kill switch. Strategy endpoints generate proposals from supplied facts and do not call providers. Sync controls report an unconfigured connector honestly rather than pretending a sync occurred.

Future LIVE proposals can be placed in the append-only approval queue through `/api/approvals` and reviewed in Audit/Risk. Approval is recorded and audited, but it does not enable execution: LIVE submission remains rejected by the gateway.
