"""Durable personal-finance operations kept separate from provider/trading namespaces."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal
_SECRET = re.compile(r"(secret|token|password|api[_-]?key|client[_-]?secret|access[_-]?token|private[_-]?key)", re.I)


def decimal(value: Any, field: str = "value") -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else D(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def money(value: Any, field: str = "amount") -> str:
    return format(decimal(value, field), "f")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def goal_progress(target: Any, current: Any, *, start: Any = 0, as_of: str | None = None) -> dict[str, str | bool]:
    target_d, current_d, start_d = decimal(target, "target"), decimal(current, "current"), decimal(start, "start")
    if target_d <= start_d:
        raise ValueError("target must be greater than start")
    progress = (current_d - start_d) / (target_d - start_d) * D("100")
    bounded = max(D(0), min(D(100), progress))
    percent = format(bounded, "f").rstrip("0").rstrip(".") or "0"
    return {"current": money(current_d), "target": money(target_d), "start": money(start_d), "percent": percent, "achieved": current_d >= target_d, "as_of": as_of or date.today().isoformat()}


def forecast_goal(target: Any, current: Any, monthly_contribution: Any, *, annual_return: Any = 0, start: Any = 0) -> dict[str, Any]:
    target_d, current_d, monthly_d, start_d = decimal(target, "target"), decimal(current, "current"), decimal(monthly_contribution, "monthly_contribution"), decimal(start, "start")
    if target_d <= start_d or monthly_d <= 0:
        raise ValueError("target must exceed start and monthly_contribution must be positive")
    rate = decimal(annual_return, "annual_return") / D(1200)
    balance = current_d
    months = 0
    while balance < target_d and months < 1200:
        balance = balance * (D(1) + rate) + monthly_d
        months += 1
    target_date = None if months is None else (date.today().replace(day=1) + __import__('datetime').timedelta(days=months * 31)).replace(day=1).isoformat()
    return {"months": months if balance >= target_d else None, "target_date": target_date, "projected_value": money(balance), "monthly_contribution": money(monthly_d), "annual_return": money(decimal(annual_return, "annual_return"))}


def _scope(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (str(payload.get("account_id", "")).strip(), str(payload.get("portfolio_id", "")).strip(), str(payload.get("namespace", "manual")).strip())


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None: return None
    result = dict(row)
    for key, value in list(result.items()):
        if key.endswith("_json") and isinstance(value, str):
            try: result[key[:-5]] = json.loads(value)
            except json.JSONDecodeError: pass
    return result


def create_goal(db: Any, payload: dict[str, Any]) -> int:
    name = str(payload.get("name", "")).strip()
    if not name: raise ValueError("name is required")
    account, portfolio, namespace = _scope(payload)
    target = money(payload["target_amount"], "target_amount")
    start = money(payload.get("starting_amount", 0), "starting_amount")
    if decimal(target) <= decimal(start): raise ValueError("target_amount must exceed starting_amount")
    now = _now()
    with db.connect() as c:
        cur = c.execute("INSERT INTO finance_goals(account_id,portfolio_id,namespace,name,target_amount,current_amount,starting_amount,currency,target_date,monthly_contribution,annual_return,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (account,portfolio,namespace,name,target,money(payload.get("current_amount", start)),start,str(payload.get("currency","INR")).upper(),payload.get("target_date"),money(payload.get("monthly_contribution",0)),money(payload.get("annual_return",0)),now,now))
        return int(cur.lastrowid)


def list_goals(db: Any, **scope: Any) -> list[dict[str, Any]]:
    clauses, params = [], []
    for key in ("account_id", "portfolio_id", "namespace"):
        if scope.get(key) is not None: clauses.append(key + "=?"); params.append(scope[key])
    with db.connect() as c: rows = c.execute("SELECT * FROM finance_goals" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY target_date, id", params).fetchall()
    result=[]
    for row in rows:
        item=_row(row); item["progress"] = goal_progress(item["target_amount"], item["current_amount"], start=item["starting_amount"]); item["forecast"] = forecast_goal(item["target_amount"], item["current_amount"], item["monthly_contribution"], annual_return=item["annual_return"], start=item["starting_amount"]) if decimal(item["monthly_contribution"]) > 0 else None; result.append(item)
    return result


def add_goal_contribution(db: Any, goal_id: int, amount: Any, *, contributed_at: str | None = None, source: str = "manual", provenance: dict[str, Any] | None = None) -> int:
    amount_s=money(amount)
    if decimal(amount_s) <= 0: raise ValueError("amount must be positive")
    now=_now()
    with db.connect() as c:
        goal=c.execute("SELECT * FROM finance_goals WHERE id=?",(goal_id,)).fetchone()
        if not goal: raise ValueError("goal not found")
        cur=c.execute("INSERT INTO goal_contributions(goal_id,amount,contributed_at,source,provenance_json,created_at) VALUES(?,?,?,?,?,?)",(goal_id,amount_s,contributed_at or date.today().isoformat(),source,json.dumps(provenance or {},sort_keys=True),now))
        c.execute("UPDATE finance_goals SET current_amount=current_amount+?, updated_at=? WHERE id=?",(amount_s,now,goal_id)); return int(cur.lastrowid)


def add_cash_flow(db: Any, payload: dict[str, Any]) -> int:
    kind=str(payload.get("kind", "")).lower();
    if kind not in {"income","expense","transfer","contribution","withdrawal"}: raise ValueError("unsupported cash-flow kind")
    account, portfolio, namespace=_scope(payload); amount=money(payload["amount"])
    with db.connect() as c:
        cur=c.execute("INSERT INTO cash_flow_records(account_id,portfolio_id,namespace,kind,amount,currency,occurred_at,category,description,source,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(account,portfolio,namespace,kind,amount,str(payload.get("currency","INR")).upper(),payload.get("occurred_at") or date.today().isoformat(),str(payload.get("category","")),str(payload.get("description","")),str(payload.get("source","manual")),json.dumps(payload.get("provenance",{}),sort_keys=True),_now())); return int(cur.lastrowid)


def add_liability(db: Any, payload: dict[str, Any]) -> int:
    name=str(payload.get("name","")).strip();
    if not name: raise ValueError("name is required")
    account,portfolio,namespace=_scope(payload)
    with db.connect() as c:
        cur=c.execute("INSERT INTO liabilities(account_id,portfolio_id,namespace,name,principal,balance,interest_rate,due_date,minimum_payment,currency,status,source,provenance_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(account,portfolio,namespace,name,money(payload.get("principal",payload["balance"])),money(payload["balance"]),money(payload.get("interest_rate",0)),payload.get("due_date"),money(payload.get("minimum_payment",0)),str(payload.get("currency","INR")).upper(),str(payload.get("status","open")),str(payload.get("source","manual")),json.dumps(payload.get("provenance",{}),sort_keys=True),_now(),_now())); return int(cur.lastrowid)


def create_alert(db: Any, payload: dict[str, Any]) -> int:
    kind=str(payload["kind"]); fingerprint=str(payload.get("fingerprint") or hashlib.sha256(json.dumps({k:payload.get(k) for k in ("kind","scope","entity_id")},sort_keys=True).encode()).hexdigest())
    with db.connect() as c:
        old=c.execute("SELECT id FROM alerts WHERE fingerprint=? AND status IN ('open','acknowledged')",(fingerprint,)).fetchone()
        if old: return int(old[0])
        cur=c.execute("INSERT INTO alerts(namespace,account_id,portfolio_id,kind,severity,title,details_json,fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(payload.get("namespace","manual"),payload.get("account_id",""),payload.get("portfolio_id",""),kind,payload.get("severity","warning"),str(payload.get("title",kind)),json.dumps(payload.get("details",{}),sort_keys=True),fingerprint,"open",_now(),_now())); return int(cur.lastrowid)


def acknowledge_alert(db: Any, alert_id: int, *, actor: str = "operator") -> bool:
    with db.connect() as c:
        cur=c.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=?, updated_at=? WHERE id=? AND status='open'",(actor,_now(),_now(),alert_id)); return cur.rowcount == 1


def set_alert_delivery(db: Any, alert_id: int, channel: str, *, state: str = "delivered", error: str = "", read: bool = False) -> None:
    if state not in {"pending", "delivered", "failed"}: raise ValueError("invalid delivery state")
    with db.connect() as c:
        c.execute("INSERT INTO alert_deliveries(alert_id,channel,state,delivered_at,error) VALUES(?,?,?,?,?) ON CONFLICT(alert_id,channel) DO UPDATE SET state=excluded.state, delivered_at=excluded.delivered_at, error=excluded.error", (alert_id, channel, state, _now() if state == "delivered" else None, error))
        if read: c.execute("UPDATE alert_deliveries SET read_at=? WHERE alert_id=? AND channel=?", (_now(), alert_id, channel))


def list_alert_deliveries(db: Any, alert_id: int) -> list[dict[str, Any]]:
    return _rows(db, "alert_deliveries", where="alert_id = " + str(int(alert_id)))


def _redact(value: Any) -> Any:
    if isinstance(value, dict): return {k: ("[REDACTED]" if _SECRET.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list): return [_redact(v) for v in value]
    if isinstance(value, str):
        try: return _redact(json.loads(value))
        except json.JSONDecodeError: return value
    return value


def backup_payload(db: Any) -> dict[str, Any]:
    tables=["finance_goals","goal_contributions","cash_flow_records","liabilities","alerts","alert_deliveries","transaction_corrections"]
    out={"format":"portfolio-tracker-finance-backup-v1","created_at":_now(),"tables":{}}
    with db.connect() as c:
        for table in tables:
            rows=[]
            for row in c.execute(f"SELECT * FROM {table}").fetchall():
                item=_redact(dict(row)); rows.append(item)
            out["tables"][table]=rows
    return out


def write_backup(db: Any, path: str | Path) -> dict[str, Any]:
    payload=backup_payload(db); raw=json.dumps(payload,sort_keys=True,indent=2).encode(); digest=hashlib.sha256(raw).hexdigest(); payload["sha256"]=digest; Path(path).write_text(json.dumps(payload,sort_keys=True,indent=2)); return {"path":str(path),"sha256":digest,"tables":{k:len(v) for k,v in payload["tables"].items()}}


def restore_backup(db: Any, path: str | Path) -> dict[str, int]:
    payload=json.loads(Path(path).read_text()); expected=payload.pop("sha256",None); raw=json.dumps(payload,sort_keys=True,indent=2).encode()
    if expected != hashlib.sha256(raw).hexdigest(): raise ValueError("backup integrity check failed")
    counts={}
    for table, rows in payload.get("tables",{}).items():
        if table not in {"finance_goals","goal_contributions","cash_flow_records","liabilities","alerts","alert_deliveries","transaction_corrections"}: continue
        with db.connect() as c:
            for row in rows:
                clean={k: (json.dumps(v, sort_keys=True) if k.endswith("_json") and isinstance(v, (dict, list)) else v) for k,v in row.items() if k not in {"id","created_at","updated_at"}}; cols=", ".join(clean); vals=", ".join("?" for _ in clean); c.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals})",tuple(clean.values()))
        counts[table]=len(rows)
    return counts


def correct_transaction(db: Any, transaction_id: int, payload: dict[str, Any], *, actor: str = "operator") -> int:
    with db.connect() as c:
        row=c.execute("SELECT * FROM transactions WHERE id=?",(transaction_id,)).fetchone()
        if not row: raise ValueError("transaction not found")
        correction_id=c.execute("INSERT INTO transaction_corrections(transaction_id,action,original_json,corrected_json,reason,actor,created_at) VALUES(?,?,?,?,?,?,?)",(transaction_id,"correction",json.dumps(dict(row),sort_keys=True),json.dumps(payload,sort_keys=True),str(payload.get("reason","manual correction")),actor,_now())).lastrowid
        return int(correction_id)


def reverse_transaction(db: Any, transaction_id: int, *, actor: str = "operator", reason: str = "manual reversal") -> int:
    return correct_transaction(db, transaction_id, {"reversal_of": transaction_id, "reason": reason}, actor=actor)


def daily_report_data(db: Any) -> dict[str, Any]:
    return {"generated_at":_now(),"goals":list_goals(db),"cash_flow":_rows(db,"cash_flow_records"),"liabilities":_rows(db,"liabilities"),"alerts":_rows(db,"alerts", where="status != 'acknowledged'")}


def _rows(db: Any, table: str, where: str = "1=1") -> list[dict[str, Any]]:
    with db.connect() as c: return [_row(r) for r in c.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC").fetchall()]
