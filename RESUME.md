# Resume Notes

Last updated: 2026-11-03

## Project
- Path: `/opt/data/portfolio-tracker`
- Latest verified code commit: `318ec01` for documentation/smoke-test work; later implementation commits are present in the local Git history.
- Full test suite last verified: 214 passed before Docker-only changes.

## Docker deployment
- Docker image: `portfolio-tracker:local`
- Container: `portfolio-tracker`
- Status: running
- Health: healthy
- Port: `5055:5055`
- Persistent volume: `portfolio_tracker_data`
- Internal `/api/report` check: HTTP 200
- Direct `docker run` deployment succeeded.
- `docker compose` deployment was not used because the Docker Compose plugin is unavailable on this host.

## Trading safety state
- Paper trading only.
- Zerodha Kite live writes disabled.
- Upstox live writes disabled.
- INDmoney is read-only via manual/CSV holdings import.
- No US execution broker/API selected or configured.
- No live broker credentials or live MCP execution path enabled.

## GitHub
- Repository has no Git remote configured yet.
- `gh` CLI is not installed/authenticated.
- Next step: user should provide the exact GitHub repository URL, then configure remote, commit Docker files, and push.

## Next action
1. Get GitHub repository URL from user.
2. Configure `origin` and authenticate safely.
3. Commit Dockerfile, docker-compose.yml, and .dockerignore.
4. Push and verify the remote branch.
