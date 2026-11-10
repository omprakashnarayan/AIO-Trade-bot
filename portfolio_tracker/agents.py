"""Local, bounded agent control plane with paper-only handlers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from .config import load_settings
from .db import Database
from .reporting import generate_report

AGENTS: tuple[dict[str, str], ...] = (
    {"name": "market_research", "mode": "deterministic", "description": "Local market research boundary; no provider connector configured."},
    {"name": "kite_short_term_proposals", "mode": "paper-proposals", "description": "Produces no-trade paper proposal status from local facts only."},
    {"name": "upstox_long_term_proposals", "mode": "paper-proposals", "description": "Produces no-trade paper proposal status from local facts only."},
    {"name": "indmoney_us_import", "mode": "read-only", "description": "Reports the INDmoney import connector honestly without network calls."},
    {"name": "risk_review", "mode": "deterministic", "description": "Reviews local risk settings and kill-switch posture."},
    {"name": "paper_trading", "mode": "paper-only", "description": "Reports paper broker readiness; never submits live orders."},
    {"name": "scheduler", "mode": "paper-only", "description": "Runs the local scheduler boundary without external strategy calls."},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _handler(name: str, db: Database, *, test: bool) -> dict[str, Any]:
    settings = load_settings()
    if name == "market_research":
        return {"status": "unconfigured_connector", "message": "No market data connector is configured; no external call was made.", "facts": 0}
    if name in {"kite_short_term_proposals", "upstox_long_term_proposals"}:
        return {"status": "no_trade", "message": "No caller-supplied market facts are queued; no proposal was generated.", "proposals": 0}
    if name == "indmoney_us_import":
        return {"status": "unconfigured_connector", "message": "INDmoney import is read-only and has no configured connector; no external call was made.", "imported": 0}
    if name == "risk_review":
        return {"status": "ok", "paper_only": True, "live_writes": False, "kill_switch": db.get_console_state("global_kill_switch", "off"), "trading_mode": settings.get("trading_mode"), "max_order_notional": settings.get("max_order_notional"), "max_position_weight": settings.get("max_position_weight")}
    if name == "paper_trading":
        return {"status": "ready", "paper_only": True, "live_writes": False, "message": "Paper broker is available; this control-plane run submits no orders."}
    if name == "scheduler":
        snapshot, _, _ = generate_report(db, save=False)
        return {"status": "ok", "generated": 0, "portfolio_positions": snapshot["summary"].get("positions", 0), "message": "No external strategy factories are registered; no provider call was made."}
    raise KeyError(name)


def install_agents(db: Database) -> None:
    db.init_db()


def list_agents(db: Database) -> list[dict[str, Any]]:
    return [dict(row) for row in db.list_agents()]


def run_agent(db: Database, name: str, *, run_type: str = "run", retry_of: int | None = None) -> dict[str, Any]:
    if name not in {item["name"] for item in AGENTS}:
        raise KeyError(name)
    agent = db.get_agent(name)
    if agent is None:
        raise KeyError(name)
    if not agent["enabled"]:
        raise PermissionError("agent is disabled")
    started = _now()
    run_id = db.create_agent_run(name, run_type=run_type, started_at=started, retry_of=retry_of)
    try:
        result = _handler(name, db, test=run_type == "test")
        result["dry_run"] = run_type == "test"
        result["run_type"] = run_type
        finished = _now()
        db.finish_agent_run(run_id, status="succeeded", finished_at=finished, result=result, log="Agent completed without external side effects.")
    except Exception as exc:
        finished = _now()
        result = {"status": "failed", "error": str(exc), "run_type": run_type, "dry_run": run_type == "test"}
        db.finish_agent_run(run_id, status="failed", finished_at=finished, result=result, log="Agent failed; failure is retained and surfaced.")
    return db.get_agent_run(run_id)


def retry_agent(db: Database, name: str) -> dict[str, Any]:
    last = db.get_last_agent_run(name)
    if last is None:
        raise ValueError("agent has no prior run to retry")
    return run_agent(db, name, run_type=str(last["run_type"]), retry_of=int(last["id"]))
