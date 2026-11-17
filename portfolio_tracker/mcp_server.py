"""Credential-free minimal MCP Streamable HTTP surface for the local app."""
from __future__ import annotations

import json, os, re, secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from .db import Database
from .execution import ExecutionGateway, LiveModeRejected
from .agents import AGENTS, run_agent
from .config import load_settings, save_settings
from .promotion_gates import PromotionGates
from .risk import RiskEngine, RiskLimits
from .trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce

_SECRET = re.compile(r"(?i)(bearer\s+|token[=:]\s*|api[_-]?key[=:]\s*|[A-Za-z0-9_-]*secret[_-]?key[=:]\s*|secret[=:]\s*|password[=:]\s*)[^\s,;]+|sk-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+")
_SECRET_KEYS = {"token", "accesstoken", "refreshtoken", "apikey", "secret", "password", "authorization", "privatekey", "clientsecret"}

def _secret_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _SECRET_KEYS or normalized.endswith(("token", "apikey", "secret", "secretkey", "password", "authorization"))

def redact(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact(parsed), separators=(",", ":"))
        return _SECRET.sub(lambda m: m.group(1) + "[REDACTED]" if m.group(1) else "[REDACTED]", value)
    if isinstance(value, dict): return {str(k): "[REDACTED]" if _secret_key(k) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [redact(v) for v in value]
    return value

def _rows(rows): return [dict(row) for row in rows]

TOOLS = [
    {"name":"health_status","description":"Local app health and paper-only status","inputSchema":{"type":"object","properties":{}} ,"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"portfolio_summary","description":"Portfolio summary","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"holdings","description":"Scoped local holdings","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"ledger_tax_report","description":"Scoped ledger and tax rows","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"market_data_status","description":"Market data cache status","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"research_findings","description":"Global stored research findings; findings have no account or portfolio association","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"finance_goals_alerts","description":"Finance goals and alerts scoped to the configured account and portfolio","inputSchema":{"type":"object","properties":{"account_id":{"type":"string"},"portfolio_id":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"provider_integration_status","description":"Provider MCP capability states; no credentials returned","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"provider_mcp_status","description":"MCP-only broker status and discovered read tools; no credentials","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"provider_mcp_snapshot","description":"Verified persisted broker MCP snapshot, read-only","inputSchema":{"type":"object","required":["broker_id","account_id"],"properties":{"broker_id":{"type":"string"},"account_id":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"paper_proposal_create","description":"Record a paper proposal (does not submit)","inputSchema":{"type":"object","required":["symbol","side","quantity"],"properties":{"symbol":{"type":"string"},"side":{"type":"string","enum":["buy","sell"]},"quantity":{"type":"string"},"portfolio_id":{"type":"string"},"account_id":{"type":"string"}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"portfolio","x-capability":"paper","x-mutating":True},
    {"name":"paper_order_submit","description":"Submit a validated market paper order through ExecutionGateway","inputSchema":{"type":"object","required":["idempotency_key","symbol","side","quantity","quote_price"],"properties":{"idempotency_key":{"type":"string"},"symbol":{"type":"string"},"side":{"type":"string","enum":["buy","sell"]},"quantity":{"type":"string"},"quote_price":{"type":"string"},"portfolio_id":{"type":"string"},"account_id":{"type":"string"},"order_type":{"type":"string","enum":["market"]}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"portfolio","x-capability":"paper","x-mutating":True},
    {"name":"kill_switch_set","description":"Set the local paper-trading kill switch","inputSchema":{"type":"object","required":["state"],"properties":{"state":{"type":"string","enum":["on","off"]}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"global","x-mutating":True},
    {"name":"scheduler_set_enabled","description":"Enable or disable the local scheduler without running it","inputSchema":{"type":"object","required":["enabled"],"properties":{"enabled":{"type":"boolean"}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"global","x-mutating":True},
    {"name":"agent_set_enabled","description":"Enable or disable an allowlisted local agent","inputSchema":{"type":"object","required":["name","enabled"],"properties":{"name":{"type":"string","enum":[agent["name"] for agent in AGENTS]},"enabled":{"type":"boolean"}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"global","x-mutating":True},
    {"name":"agent_run","description":"Run an enabled allowlisted local agent","inputSchema":{"type":"object","required":["name"],"properties":{"name":{"type":"string","enum":[agent["name"] for agent in AGENTS]},"run_type":{"type":"string","enum":["run","test"]}}},"annotations":{"readOnlyHint":False,"destructiveHint":False,"openWorldHint":False},"x-scope":"global","x-mutating":True},
    {"name":"promotion_gate_status","description":"Promotion gate checklist status","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
]

class MCPServer:
    def __init__(self, db_factory: Callable[[], Database] = Database): self.db_factory = db_factory
    def _db(self):
        db = self.db_factory(); db.init_db(); return db
    def _scope(self, name, args):
        settings = load_settings() if name in {"paper_order_submit", "paper_proposal_create", "finance_goals_alerts", "holdings", "portfolio_summary", "ledger_tax_report"} else {}
        configured_account = os.environ.get("PORTFOLIO_MCP_ACCOUNT_ID") or settings.get("default_account_id") or None
        configured_portfolio = (os.environ.get("PORTFOLIO_MCP_PORTFOLIO_ID") or os.environ.get("PORTFOLIO_MCP_PORTFOLIO") or settings.get("default_portfolio_id") or None)
        if configured_account and args.get("account_id") and str(args["account_id"]) != configured_account:
            raise PermissionError("requested namespace is outside MCP token scope")
        for key in ("portfolio_id", "portfolio"):
            if configured_portfolio and args.get(key) and str(args[key]) != configured_portfolio:
                raise PermissionError("requested namespace is outside MCP token scope")
        if name in {"paper_order_submit", "paper_proposal_create", "finance_goals_alerts"}:
            return {**args, "account_id": args.get("account_id") or configured_account,
                    "portfolio_id": args.get("portfolio_id") or args.get("portfolio") or configured_portfolio}
        if name in {"holdings", "portfolio_summary", "ledger_tax_report"}:
            return {**args, "portfolio": args.get("portfolio") or configured_portfolio}
        return args
    def call(self, name: str, args: dict[str, Any], actor: str = "mcp") -> Any:
        args = self._scope(name, args)
        db = self._db()
        if name == "health_status": return {"status":"ok","paper_only":True,"live_tools":False,"timestamp":datetime.now(timezone.utc).isoformat()}
        if name == "holdings":
            with db.connect() as conn:
                rows=conn.execute("SELECT * FROM holdings" + (" WHERE portfolio=?" if args.get("portfolio") else "") + " ORDER BY portfolio,symbol", (args["portfolio"],) if args.get("portfolio") else ()).fetchall()
            return {"holdings": _rows(rows)}
        if name == "portfolio_summary":
            with db.connect() as conn:
                rows=conn.execute("SELECT * FROM holdings" + (" WHERE portfolio=?" if args.get("portfolio") else ""), (args["portfolio"],) if args.get("portfolio") else ()).fetchall()
            return {"holdings_count":len(rows),"total_cost":str(sum(Decimal(str(r["shares"])) * Decimal(str(r["avg_cost"])) for r in rows))}
        if name == "ledger_tax_report":
            with db.connect() as conn:
                scope = " WHERE portfolio=?" if args.get("portfolio") else ""; params=(args["portfolio"],) if args.get("portfolio") else ()
                events = conn.execute("SELECT * FROM financial_events" + scope + " ORDER BY id DESC LIMIT 500", params).fetchall()
                lots = conn.execute("SELECT * FROM tax_lots" + scope + " ORDER BY id DESC LIMIT 500", params).fetchall()
            return {"financial_events":_rows(events),"tax_lots":_rows(lots)}
        if name == "market_data_status":
            with db.connect() as conn: rows=conn.execute("SELECT * FROM market_price_snapshots ORDER BY id DESC LIMIT 10").fetchall()
            return {"status":"local-cache","snapshots":_rows(rows)}
        if name == "research_findings":
            with db.connect() as conn:
                rows=conn.execute("SELECT * FROM research_findings" + (" WHERE symbol=?" if args.get("symbol") else "") + " ORDER BY id DESC LIMIT 500", (args["symbol"],) if args.get("symbol") else ()).fetchall()
            return {"findings":_rows(rows)}
        if name == "finance_goals_alerts":
            with db.connect() as conn:
                clauses, params = [], []
                for key in ("account_id", "portfolio_id"):
                    if args.get(key): clauses.append(f"{key}=?"); params.append(args[key])
                scope = " WHERE " + " AND ".join(clauses) if clauses else ""
                goals=conn.execute("SELECT * FROM finance_goals" + scope + " ORDER BY id DESC", params).fetchall()
                alerts=conn.execute("SELECT * FROM alerts" + scope + " ORDER BY id DESC", params).fetchall()
            return {"goals":_rows(goals),"alerts":_rows(alerts)}
        if name in {"provider_mcp_status", "provider_integration_status"}:
            with db.connect() as conn:
                runs = conn.execute("SELECT broker_id,account_id,provider,status,finished_at,error_json FROM provider_sync_runs ORDER BY id DESC LIMIT 50").fetchall()
            latest = {}
            for row in runs:
                key = (row["broker_id"], row["account_id"])
                if key not in latest: latest[key] = dict(row)
            providers = [{"broker_id": k[0], "account_id": k[1], "provider": v["provider"], "state": "connected" if v["status"] == "succeeded" else v["status"], "policy": "mcp_only read-only", "last_sync_status": v["status"], "last_sync": v["finished_at"], "error": redact(json.loads(v["error_json"] or "{}"))} for k, v in latest.items()]
            return {"providers": providers, "mode": "mcp_only", "live_writes": False} if name == "provider_mcp_status" else (providers or {"state": "unconfigured", "policy": "mcp_only read-only; authorization and verified discovery required"})
        if name == "provider_mcp_snapshot":
            broker_id, account_id = str(args.get("broker_id", "")), str(args.get("account_id", ""))
            if broker_id not in {"kite", "upstox", "indmoney"} or not account_id: raise PermissionError("invalid provider snapshot scope")
            return {"broker_id": broker_id, "account_id": account_id, "holdings": _rows(db.list_provider_holdings(broker_id=broker_id, account_id=account_id)), "cash": _rows(db.list_provider_cash(broker_id=broker_id, account_id=account_id)), "orders": _rows(db.list_provider_orders(broker_id=broker_id, account_id=account_id)), "fills": _rows(db.list_provider_fills(broker_id=broker_id, account_id=account_id)), "source": "verified_mcp_persisted_snapshot", "live_writes": False}
        if name == "promotion_gate_status": return PromotionGates(db).status()
        if name == "paper_proposal_create":
            payload = {k:args.get(k) for k in ("symbol","side","quantity","portfolio_id","account_id")}; payload["mode"]="paper"; payload["created_at"]=datetime.now(timezone.utc).isoformat()
            db.append_audit_event(event_type="mcp.paper_proposal_created", entity_type="proposal", entity_id=str(args.get("symbol")), payload=payload, account_id=actor, mode="paper")
            return payload
        if name == "paper_order_submit":
            if args.get("order_type", "market") != "market" or "limit_price" in args:
                raise ValueError("MCP paper orders support market orders only")
            intent = OrderIntent(idempotency_key=str(args["idempotency_key"]), portfolio_id=args.get("portfolio_id"), account_id=args.get("account_id"), symbol=str(args["symbol"]), side=OrderSide(args["side"]), order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, quantity=Decimal(str(args["quantity"])), currency=str(args.get("currency","INR")), mode=ExecutionMode.PAPER)
            settings = load_settings()
            limits = RiskLimits(global_kill_switch=db.get_console_state("global_kill_switch", "off") == "on", max_order_notional=Decimal(str(settings["max_order_notional"])) if settings.get("max_order_notional") not in (None, "") else None, max_position_weight=Decimal(str(settings["max_position_weight"])) if settings.get("max_position_weight") not in (None, "") else None)
            result = ExecutionGateway(db, risk=RiskEngine(limits=limits, db=db)).submit(intent, quote_price=Decimal(str(args["quote_price"])))
            db.append_audit_event(event_type="mcp.paper_order_called", entity_type="order_intent", entity_id=intent.idempotency_key, payload={"actor":actor,"status":result.status}, account_id=actor, portfolio_id=intent.portfolio_id, mode="paper")
            return {"status":result.status,"submitted":result.submitted,"error":result.error,"risk":result.risk.message}
        if name == "kill_switch_set":
            state = args.get("state")
            if state not in {"on", "off"}: raise ValueError("state must be on or off")
            db.set_console_state_with_audit("global_kill_switch", state, event_type="risk.kill_switch.changed", entity_type="risk", entity_id="global", payload={"state":state,"actor":actor}, account_id=actor, mode="paper")
            return {"global_kill_switch":state == "on"}
        if name == "scheduler_set_enabled":
            enabled = args.get("enabled")
            if not isinstance(enabled, bool): raise ValueError("enabled must be a boolean")
            original_settings = load_settings()
            settings = {**original_settings, "scheduler_enabled": enabled}
            db.append_audit_event(event_type="scheduler.state.change_requested", entity_type="scheduler", entity_id="operator", payload={"enabled":enabled,"actor":actor}, account_id=actor, mode="paper")
            try:
                save_settings(settings)
                db.append_audit_event(event_type="scheduler.state.changed", entity_type="scheduler", entity_id="operator", payload={"enabled":enabled,"actor":actor}, account_id=actor, mode="paper")
            except Exception:
                try:
                    save_settings(original_settings)
                except Exception:
                    pass
                raise
            return {"enabled":enabled}
        if name == "agent_set_enabled":
            agent_name, enabled = args.get("name"), args.get("enabled")
            if agent_name not in {item["name"] for item in AGENTS}: raise KeyError("unknown agent")
            if not isinstance(enabled, bool): raise ValueError("enabled must be a boolean")
            if not db.set_agent_enabled_with_audit(agent_name, enabled, event_type="mcp.agent.enabled", entity_type="agent", entity_id=agent_name, payload={"enabled":enabled,"actor":actor}, account_id=actor, mode="paper"):
                raise KeyError("unknown agent")
            return {"name":agent_name,"enabled":enabled}
        if name == "agent_run":
            agent_name = args.get("name")
            if agent_name not in {item["name"] for item in AGENTS}: raise KeyError("unknown agent")
            run_type = args.get("run_type", "run")
            if run_type not in {"run", "test"}: raise ValueError("run_type must be run or test")
            run = run_agent(db, agent_name, run_type=run_type)
            db.append_audit_event(event_type="mcp.agent.run", entity_type="agent_run", entity_id=str(run["id"]), payload={"agent_name":agent_name,"status":run["status"],"run_type":run_type,"actor":actor}, account_id=actor, mode="paper")
            return run
        raise KeyError("unknown tool")
    def rpc(self, request: dict[str, Any], actor: str = "mcp") -> dict[str, Any] | None:
        rid = request.get("id")
        method = request.get("method")
        try:
            if method == "initialize": result={"protocolVersion":"2025-03-26","capabilities":{"tools":{"listChanged":False},"resources":{"subscribe":False,"listChanged":False}},"serverInfo":{"name":"portfolio-tracker","version":"phase-6"}}
            elif method == "notifications/initialized": return None
            elif method == "tools/list": result={"tools":TOOLS}
            elif method == "resources/list": result={"resources":[{"uri":"portfolio://promotion-gates","name":"Promotion gates","mimeType":"application/json"}]}
            elif method == "resources/read":
                if request.get("params",{}).get("uri") != "portfolio://promotion-gates": raise KeyError("unknown resource")
                result={"contents":[{"uri":"portfolio://promotion-gates","mimeType":"application/json","text":json.dumps(self.call("promotion_gate_status",{},actor))}]}
            elif method == "tools/call":
                params=request.get("params",{}); name=params.get("name"); result={"content":[{"type":"text","text":json.dumps(redact(self.call(name, params.get("arguments") or {}, actor)), separators=(",",":"))}],"isError":False}
            else: raise KeyError("method not found")
            return {"jsonrpc":"2.0","id":rid,"result":result} if rid is not None else None
        except Exception as exc:
            return {"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":redact(str(exc))}} if rid is not None else None

def token_valid(request) -> bool:
    configured=os.environ.get("PORTFOLIO_MCP_API_TOKEN", "").strip()
    supplied=request.headers.get("Authorization","").removeprefix("Bearer ").strip() or request.headers.get("X-MCP-API-Token","").strip()
    return bool(configured and supplied and secrets.compare_digest(configured,supplied))
