"""Credential-free minimal MCP Streamable HTTP surface for the local app."""
from __future__ import annotations

import json, os, re, secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from .db import Database
from .execution import ExecutionGateway, LiveModeRejected
from .promotion_gates import PromotionGates
from .trading_models import ExecutionMode, OrderIntent, OrderSide, OrderType, TimeInForce

_SECRET = re.compile(r"(?i)(bearer\s+|token[=:]\s*|api[_-]?key[=:]\s*|secret[=:]\s*|password[=:]\s*)[^\s,;]+|sk-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+")

def redact(value: Any) -> Any:
    if isinstance(value, str): return _SECRET.sub(lambda m: m.group(1) + "[REDACTED]" if m.group(1) else "[REDACTED]", value)
    if isinstance(value, dict): return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [redact(v) for v in value]
    return value

def _rows(rows): return [dict(row) for row in rows]

TOOLS = [
    {"name":"health_status","description":"Local app health and paper-only status","inputSchema":{"type":"object","properties":{}} ,"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"portfolio_summary","description":"Portfolio summary","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"holdings","description":"Scoped local holdings","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"ledger_tax_report","description":"Scoped ledger and tax rows","inputSchema":{"type":"object","properties":{"portfolio":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"market_data_status","description":"Market data cache status","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"research_findings","description":"Stored research findings","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"finance_goals_alerts","description":"Scoped finance goals and alerts","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"portfolio"},
    {"name":"provider_integration_status","description":"Provider capability states; no credentials returned","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
    {"name":"paper_proposal_create","description":"Record a paper proposal (does not submit)","inputSchema":{"type":"object","required":["symbol","side","quantity"],"properties":{"symbol":{"type":"string"},"side":{"type":"string","enum":["buy","sell"]},"quantity":{"type":"string"},"portfolio_id":{"type":"string"},"account_id":{"type":"string"}}},"x-scope":"portfolio","x-capability":"paper"},
    {"name":"paper_order_submit","description":"Submit a validated paper order through ExecutionGateway","inputSchema":{"type":"object","required":["idempotency_key","symbol","side","quantity","quote_price"],"properties":{"idempotency_key":{"type":"string"},"symbol":{"type":"string"},"side":{"type":"string","enum":["buy","sell"]},"quantity":{"type":"string"},"quote_price":{"type":"string"},"portfolio_id":{"type":"string"},"account_id":{"type":"string"},"order_type":{"type":"string","enum":["market","limit"]},"limit_price":{"type":"string"}}},"x-scope":"portfolio","x-capability":"paper","x-mutating":True},
    {"name":"promotion_gate_status","description":"Promotion gate checklist status","inputSchema":{"type":"object","properties":{}},"annotations":{"readOnlyHint":True},"x-scope":"global"},
]

class MCPServer:
    def __init__(self, db_factory: Callable[[], Database] = Database): self.db_factory = db_factory
    def _db(self):
        db = self.db_factory(); db.init_db(); return db
    def _scope(self, args):
        for key in ("account_id", "portfolio_id", "portfolio"):
            configured = os.environ.get("PORTFOLIO_MCP_" + key.upper())
            if configured and args.get(key) and str(args[key]) != configured: raise PermissionError("requested namespace is outside MCP token scope")
    def call(self, name: str, args: dict[str, Any], actor: str = "mcp") -> Any:
        self._scope(args)
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
                goals=conn.execute("SELECT * FROM finance_goals ORDER BY id DESC").fetchall(); alerts=conn.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
            return {"goals":_rows(goals),"alerts":_rows(alerts)}
        if name == "provider_integration_status": return {"kite":{"state":"unconfigured","policy":"read-only; discovered transport required"},"upstox":{"state":"unconfigured","policy":"read-only; discovered transport required"},"indmoney":{"state":"read-only","policy":"manual/MCP handoff only"}}
        if name == "promotion_gate_status": return PromotionGates(db).status()
        if name == "paper_proposal_create":
            payload = {k:args.get(k) for k in ("symbol","side","quantity","portfolio_id","account_id")}; payload["mode"]="paper"; payload["created_at"]=datetime.now(timezone.utc).isoformat()
            db.append_audit_event(event_type="mcp.paper_proposal_created", entity_type="proposal", entity_id=str(args.get("symbol")), payload=payload, account_id=actor, mode="paper")
            return payload
        if name == "paper_order_submit":
            intent = OrderIntent(idempotency_key=str(args["idempotency_key"]), portfolio_id=args.get("portfolio_id") or "Main", account_id=args.get("account_id"), symbol=str(args["symbol"]), side=OrderSide(args["side"]), order_type=OrderType(args.get("order_type","market")), time_in_force=TimeInForce.DAY, quantity=Decimal(str(args["quantity"])), currency=str(args.get("currency","INR")), mode=ExecutionMode.PAPER, limit_price=args.get("limit_price"))
            result = ExecutionGateway(db).submit(intent, quote_price=Decimal(str(args["quote_price"])))
            db.append_audit_event(event_type="mcp.paper_order_called", entity_type="order_intent", entity_id=intent.idempotency_key, payload={"actor":actor,"status":result.status}, account_id=actor, portfolio_id=intent.portfolio_id, mode="paper")
            return {"status":result.status,"submitted":result.submitted,"error":result.error,"risk":result.risk.message}
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
